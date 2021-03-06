#!/usr/bin/env python
# Run the SAS code on the WRDS server and get the result
import pandas as pd
import wrds
import re, subprocess, os, paramiko
import logging
import warnings
from io import StringIO
from time import gmtime, strftime
from sqlalchemy import create_engine
from os import getenv

client = paramiko.SSHClient()
wrds_id = getenv("WRDS_ID")
wrds_pass = getenv("WRDS_PASS")

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("wrds2postgres")
logger.setLevel(getenv("LOGLEVEL", "DEBUG"))

warnings.filterwarnings(action='ignore',module='.*paramiko.*')

def get_process(sas_code, wrds_id=None, fpath=None):

    if wrds_id:
        """Function runs SAS code on WRDS server and
        returns result as pipe on stdout."""
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy()) 
        client.connect('wrds-cloud.wharton.upenn.edu',
                       username=wrds_id, password=wrds_pass, compress=True)
        stdin, stdout, _ = client.exec_command("qsas -stdio -noterminal | iconv -f LATIN1 -t UTF8")
        stdin.write(sas_code)
        stdin.close()

        channel = stdout.channel
        # indicate that we're not going to write to that channel anymore
        channel.shutdown_write()
        return stdout

    elif fpath:

        p=subprocess.Popen(['sas', '-stdio', '-noterminal'],
                           stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        p.stdin.write(sas_code)
        p.stdin.close()

        return p.stdout

def code_row(row):

    """A function to code PostgreSQL data types using output from SAS's PROC CONTENTS."""

    format_ = row['format']
    formatd = row['formatd']
    formatl = row['formatl']
    col_type = row['type']

    if col_type==2:
        return 'text'

    if not pd.isnull(format_):
        if re.search(r'datetime', format_, re.I):
            return 'timestamp'
        elif format_ =='TIME8.' or re.search(r'time', format_, re.I) or format_=='TOD':
            return "time"
        elif re.search(r'(date|yymmdd|mmddyy)', format_, re.I):
            return "date"

    if format_ == "BEST":
        return 'float8'
    if formatd != 0:
        return 'float8'
    if formatd == 0 and formatl != 0:
        return 'int8'
    if formatd == 0 and formatl == 0:
        return 'float8'
    else:
        return 'text'

################################################
# 1. Get format of variables on WRDS table     #
################################################
# SAS code to extract information about the datatypes of the SAS data.
# Note that there are some date formates that don't work with this code.
def get_row_sql(row):
    """Function to get SQL to create column from row in PROC CONTENTS."""
    postgres_type = row['postgres_type']
    if postgres_type == 'timestamp':
        postgres_type = 'text'

    return row['name'].lower() + ' ' + postgres_type

def sas_to_pandas(sas_code, wrds_id, fpath):

    """Function that runs SAS code on WRDS or local server
    and returns a Pandas data frame."""
    p = get_process(sas_code, wrds_id, fpath)

    if wrds_id:
        df = pd.read_csv(StringIO(p.read().decode('utf-8')))
    else:
        df = pd.read_csv(StringIO(p.read()))
    df.columns = map(str.lower, df.columns)
    p.close()

    return(df)

def get_table_sql(table_name, schema, wrds_id=None, fpath=None, drop="", keep="", rename="", return_sql=True, \
                  alt_table_name=None):
    if fpath:
        libname_stmt = "libname %s '%s';" % (schema, fpath)
    else:
        libname_stmt = ""

    if not alt_table_name:
        alt_table_name = table_name

    sas_template = """
        options nonotes nosource;
        %s

        * Use PROC CONTENTS to extract the information desired.;
        proc contents data=%s.%s(%s %s obs=1 %s) out=schema noprint;
        run;

        proc sort data=schema;
            by varnum;
        run;

        * Now dump it out to a CSV file;
        proc export data=schema(keep=name format formatl formatd length type)
            outfile=stdout dbms=csv;
        run;
    """

    if rename != '':
        rename_str = "rename=(" + rename + ") "
    else:
        rename_str = ""

    if drop != '':
        drop_str = "drop=" + drop 
    else:
        drop_str = ""
        
    if keep != '':
        keep_str = "keep=" + keep 
    else:
        keep_str = ""

    sas_code = sas_template % (libname_stmt, schema, table_name, drop_str, keep_str, rename_str)
    
    # Run the SAS code on the WRDS server and get the result
    df = sas_to_pandas(sas_code, wrds_id, fpath)
    df['postgres_type'] = df.apply(code_row, axis=1)
    make_table_sql = "CREATE TABLE " + schema + "." + alt_table_name + " (" + \
                      df.apply(get_row_sql, axis=1).str.cat(sep=", ") + ")"

    # Identify the datetime fields. These need special handling.
    datatimes = df.loc[df['postgres_type']=="timestamp", "name"]
    datetime_cols = [field.lower() for field in datatimes]

    if return_sql:
        return {"sql":make_table_sql, "datetimes":datetime_cols}
    else:
        df['name'] = df['name'].str.lower()
        return df

def get_wrds_process(table_name, schema, wrds_id=None, fpath=None,
                     drop="", keep="", fix_cr = False, fix_missing = False, obs="", rename=""):
    if fix_cr:
        fix_missing = True
        fix_cr_code = """
            array _char _character_;
            do over _char;
                _char = compress(_char, , 'kw');
            end;"""
    else:
        fix_cr_code = ""

    if fpath:
        libname_stmt = "libname %s '%s';" % (schema, fpath)
    else:
        libname_stmt = ""

    if rename != '':
        rename_str = " rename=(" + rename + ")"
    else:
        rename_str = ""

    if fix_missing or drop != '' or obs != '' or keep !='':
        # If need to fix special missing values, then convert them to
        # regular missing values, then run PROC EXPORT
        if table_name == "dsf":
            dsf_fix = "format numtrd 8.;\n"
        else:
            dsf_fix = ""

        if obs != "":
            obs_str = " obs=" + str(obs)
        else:
            obs_str = ""

        drop_str = "drop=" + drop + " "
        keep_str = "keep=" + keep + " "
        
        if keep:
            print(keep_str)
        
        if obs != '' or drop != '' or rename != '' or keep != '':
            sas_table = table_name + "(" + drop_str + keep_str + obs_str + rename_str + ")"
        else:
            sas_table = table_name

        if table_name == "fund_names":
            fund_names_fix = """
                proc sql;
                    DELETE FROM %s%s
                    WHERE prxmatch('\\D', first_offer_dt) ge 1;
                quit;""" % (wrds_id, table_name)
        else:
            fund_names_fix = ""

        # Cut table name to no more than 32 characters
        # (A SAS limitation)
        new_table = "%s%s" % (schema, table_name)
        new_table = new_table[0:min(len(new_table), 32)]
        
        sas_template = """
            options nosource nonotes;

            %s

            * Fix missing values;
            data %s;
                set %s.%s;

                * dsf_fix;
                %s

                * fix_cr_code;
                %s

                array allvars _numeric_ ;

                do over allvars;
                  if missing(allvars) then allvars = . ;
                end;
            run;

            * fund_names_fix;
            %s

            proc export data=%s outfile=stdout dbms=csv;
            run;"""
        sas_code = sas_template % (libname_stmt, new_table, schema, sas_table, dsf_fix,
                                   fix_cr_code, fund_names_fix, new_table)
                                   
    else:

        sas_template = """
            options nosource nonotes;
            %s

            proc export data=%s.%s(%s) outfile=stdout dbms=csv;
            run;"""

        sas_code = sas_template % (libname_stmt, schema, table_name, rename_str)

    p = get_process(sas_code, wrds_id=wrds_id, fpath=fpath)
    return(p)

def wrds_to_pandas(table_name, schema, wrds_id, rename="", obs=None):

    p = get_wrds_process(table_name, schema, wrds_id, rename=rename, obs=obs)
    df = pd.read_csv(StringIO(p.read().decode('utf-8')))
    df.columns = map(str.lower, df.columns)
    p.close()

    return(df)

def get_modified_str(table_name, schema, wrds_id):
    sas_code = "proc contents data=" + schema + "." + table_name + ";"
    contents = get_process(sas_code, wrds_id).readlines()
    modified = ""

    next_row = False
    for line in contents:
        if next_row:
            line = re.sub(r"^\s+(.*)\s+$", r"\1", line)
            line = re.sub(r"\s+$", "", line)
            if not re.findall(r"Protection", line):
              modified += " " + line.rstrip()
            next_row = False

        if re.match(r"Last Modified", line):
            modified = re.sub(r"^Last Modified\s+(.*?)\s{2,}.*$", r"Last modified: \1", line)
            modified = modified.rstrip()
            next_row = True

    return modified

def get_table_comment(table_name, schema, engine):

    if engine.dialect.has_table(engine.connect(), table_name, schema=schema):
        sql = """SELECT obj_description('"%s"."%s"'::regclass, 'pg_class')""" % (schema, table_name)
        res = engine.execute(sql).fetchone()[0]
        return(res)
    else:
        return ""

def set_table_comment(table_name, schema, comment, engine):

    connection = engine.connect()
    trans = connection.begin()
    sql = """
        COMMENT ON TABLE "%s"."%s" IS '%s'""" % (schema, table_name, comment)

    try:
        connection.execute(sql)
        trans.commit()
    except:
        trans.rollback()
        raise

    return True

def wrds_to_pg(table_name, schema, engine, wrds_id=None,
               fpath=None, fix_missing=False, fix_cr=False, drop="", obs="", rename="", keep="",
               alt_table_name = None):

    if not alt_table_name:
        alt_table_name = table_name

    make_table_data = get_table_sql(table_name=table_name, wrds_id=wrds_id,
                                    fpath=fpath, schema=schema, drop=drop, rename=rename, keep=keep,
                                    alt_table_name=alt_table_name)

    #res = engine.execute("CREATE SCHEMA IF NOT EXISTS " + schema)
    res = engine.execute("DROP TABLE IF EXISTS " + schema + "." + alt_table_name + " CASCADE")
    res = engine.execute(make_table_data["sql"])

    now = strftime("%H:%M:%S", gmtime())
    print("Beginning file import at %s." % now)
    print("Importing data into %s.%s" % (schema, alt_table_name))
    p = get_wrds_process(table_name=table_name, fpath=fpath, schema=schema, wrds_id=wrds_id,
				drop=drop, keep=keep, fix_cr=fix_cr, fix_missing = fix_missing, obs=obs, rename=rename)

    res = wrds_process_to_pg(alt_table_name, schema, engine, p)
    now = strftime("%H:%M:%S", gmtime())
    print("Completed file import at %s." % now)

    for var in make_table_data["datetimes"]:
        print("Fixing %s" % var)
        sql = r"""
            ALTER TABLE "%s"."%s"
            ALTER %s TYPE timestamp
            USING regexp_replace(%s, '(\d{2}[A-Z]{3}\d{4}):', '\1 ' )::timestamp""" % (schema, alt_table_name, var, var)
        engine.execute(sql)

    return res

def wrds_process_to_pg(table_name, schema, engine, p):
    # The first line has the variable names ...
    
    var_names = p.readline().rstrip().lower().split(sep=",")
    
    # ... the rest is the data
    copy_cmd =  "COPY " + schema + "." + table_name + " (" + ", ".join(var_names) + ")"
    copy_cmd += " FROM STDIN CSV ENCODING 'utf-8'"
    
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SET DateStyle TO 'ISO, MDY'")
        cursor.copy_expert(copy_cmd, p)
        cursor.close()
        connection.commit()
    finally:
        connection.close()
        p.close()
    return True

def list_schemas(wrds_id=os.getenv("WRDS_ID")):
    db = wrds.Connection(wrds_username=wrds_id)
    libraries = db.list_libraries()
    db.close()
    return libraries

def list_tables(schema, wrds_id=os.getenv("WRDS_ID")):
    db = wrds.Connection(wrds_username=wrds_id)
    tables = db.list_tables(schema)
    db.close()
    return tables

def list_accessible_tables(schema, wrds_id=os.getenv("WRDS_ID")):
    db = wrds.Connection(wrds_username=wrds_id)
    tables = db.list_tables(schema)
    accessible, non_accessible = [], []
    logger.info(f"Checking permission on {len(tables)} tables in schema {schema}")
    for table in tables:
        try:
            logger.debug(f"Checking {table}")
            db.get_table(schema, table, obs=1)
            accessible.append(table)
            logger.debug(f"{table} is accessible")
        except Exception as e:
            if "InsufficientPrivilege" in str(e):
                non_accessible.append(table)
                logger.debug(f"{table} is not accessible")
            else:
                raise
    db.close()
    logger.info(f"{len(accessible)}/{len(tables)} tables are accessible, {len(non_accessible)}/{len(tables)} tables are not.")
    return accessible, non_accessible

def wrds_update(table_name, schema, host=os.getenv("PGHOST"), dbname=os.getenv("PGDATABASE"), engine=None, 
        wrds_id=os.getenv("WRDS_ID"), fpath=None, force=False, fix_missing=False, fix_cr=False, drop="", keep="", 
        obs="", rename="", alt_table_name=None):
          
    if not alt_table_name:
        alt_table_name = table_name
          
    if not engine:
        if not (host and dbname):
            print("Error: Missing connection variables. Please specify engine or (host, dbname).")
            quit()
        else:
            engine = create_engine("postgresql://" + host + "/" + dbname) 
    if wrds_id:
        # 1. Get comments from PostgreSQL database
        comment = get_table_comment(alt_table_name, schema, engine)
        
        # 2. Get modified date from WRDS
        modified = get_modified_str(table_name, schema, wrds_id)
    else:
        comment = 'Updated on ' + strftime("%Y-%m-%d %H:%M:%S", gmtime())
        modified = comment
        # 3. If updated table available, get from WRDS
    if modified == comment and not force and not fpath:
        print(schema + "." + alt_table_name + " already up to date")
        return False
    elif modified == "" and not force:
        print("WRDS flaked out!")
        return False
    else:
        if fpath:
            print("Importing local file.")
        elif force:
            print("Forcing update based on user request.")
        else:
            print("Updated %s.%s is available." % (schema, table_name))
            print("Getting from WRDS.\n")
        wrds_to_pg(table_name=table_name, schema=schema, engine=engine, wrds_id=wrds_id,
                fpath=fpath, fix_missing=fix_missing, fix_cr=fix_cr,
                drop=drop, keep=keep, obs=obs, rename=rename, alt_table_name=alt_table_name)
        set_table_comment(alt_table_name, schema, modified, engine)
        
        if not role_exists(engine, schema):
            create_role(engine, schema)
        
        sql = r"""
            ALTER TABLE "%s"."%s" OWNER TO %s""" % (schema, alt_table_name, schema)
        engine.execute(sql)

        if not role_exists(engine, "%s_access" % schema):
            create_role(engine, "%s_access" % schema)
            
        sql = r"""
            GRANT SELECT ON "%s"."%s"  TO %s_access""" % (schema, alt_table_name, schema)
        engine.execute(sql)

        return True

def run_file_sql(file, engine):
    f = open(file, 'r')
    sql = f.read()
    print("Running SQL in %s" % file)
    res = engine.execute(sql)
    res.close()

def make_engine(host=None, dbname=None, wrds_id=None):
    if not dbname:
      dbname = getenv("PGDATABASE")
    if not host:
      host = getenv("PGHOST", "localhost")
    if not wrds_id:
      wrds_id = getenv("WRDS_ID")
    
    engine = create_engine("postgresql://" + host + "/" + dbname)
    return engine
  
def role_exists(engine, role):
    res = engine.execute("SELECT COUNT(*) FROM pg_roles WHERE rolname='%s'" % role)
    rs = [r[0] for r in res]
    
    return rs[0] > 0

def create_role(engine, role):
    engine.execute("CREATE ROLE %s" % role)
    return True
