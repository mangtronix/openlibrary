#! /usr/bin/env python
"""Script to install OL dev instance.
"""
import ConfigParser
import os, shutil
import shlex
import subprocess
import sys
import time
import urllib, urllib2
import commands

config = None
INTERP = None

class Path:
    """Wrapper over file path, inspired by py.path.local.
    """
    def __init__(self, path):
        self.path = path
        
    def exists(self):
        return os.path.exists(self.path)
        
    def basename(self):
        return os.path.basename(self.path)
    
    def join(self, *a):
        parts = [self.path] + list(a)
        path = os.path.join(*parts)
        return Path(path)
        
    def mkdir(self, *parts):
        path = self.join(*parts).path
        if not os.path.exists(path):
            os.makedirs(path)
            
    def isdir(self):
        return os.path.isdir(self.path)
        
    def copy_to(self, dest, recursive=False):
        if isinstance(dest, Path):
            dest = dest.path
            
        options = ""
        if recursive:
            options += " -r"
            
        cmd = "cp %s %s %s" % (options, self.path, dest)
        os.system(cmd)
        
    def read(self):
        return open(self.path).read()
        
    def write(self, text, append=False):
        if append:
            f = open(self.path, 'a')
        else:
            f = open(self.path, 'w')
        f.write(text)
        f.close()

CWD = Path(os.getcwd())

## Common utilities
def log(level, args):
    msg = " ".join(map(str, args))
    if level == "ERROR" or level == "INFO":
        print msg

    text = time.asctime() + " " + level.ljust(6) + " " + msg + "\n"
    CWD.join("var/log/install.log").write(text, append=True)
    
def info(*args):
    log("INFO", args)
    
def debug(*args):
    log("DEBUG", args)
    
def error(*args):
    log("ERROR", args)
    
def write(path, text, append=False):
    if append:
        f = open(path, "a")
    else:
        f = open(path, "w")
    f.write(text)
    f.close()
    
def system(cmd):
    debug("Executing %r" % cmd)
    ret = os.system(">>var/log/install.log 2>&1 " + cmd)
    if ret != 0:
        raise Exception("%r failed with exit code %d" % (cmd, ret))

def setup_dirs():
    dirs = (
        "var/cache var/lib var/log var/run" +
        " var/lib/coverstore/localdisk" +
        " usr/local/bin usr/local/etc usr/local/lib"
    )
    os.system("mkdir -p " + dirs)
    os.system("echo > var/log/install.log")

def read_config():
    """Reads conf/install.ini file.
    """
    info("reading config file config/install.ini")
    p = ConfigParser.ConfigParser()
    p.read("conf/install.ini")
    return dict(p.items("install"))
    
def download_and_extract(url, dirname=None):
    path = CWD.join("var/cache", url.split("/")[-1])
    if not path.exists():
        system("wget %s -O %s" % (url, path.path))

    dirname = dirname or path.basename().replace(".tgz", "").replace(".tar.gz", "")
    if not CWD.join("usr/local", dirname).exists():
        system("cd usr/local && tar xzf " + path.path)

def find_distro():
    uname = os.uname()[0]
    if uname == "Darwin":
        return "osx"
    else:
        return "linux"
    
class Process:
    def __init__(self):
        self.process = None
        
    def start(self):
        info("    starting", self.__class__.__name__.lower())
        
        specs = self.get_specs()
        stdout = open("var/log/install.log", 'a')
        specs["stdout"] = stdout
        specs["stderr"] = stdout
        
        command = specs.pop("command")
        args = command.split()
        self.process = subprocess.Popen(args, **specs)
        time.sleep(2)
        
    def stop(self):
        info("    stopping", self.__class__.__name__.lower())
        self.process and self.process.terminate()
        
    def run_tasks(self, *tasks):
        try:
            self.start()
            for task in tasks:
                task()
        finally:
            self.stop()
            
class Postgres(Process):
    def get_specs(self):
        return {
            "command": "usr/local/postgresql-8.4.4/bin/postgres -D var/lib/postgresql"
        }
                        
class CouchDB(Process):
    def get_specs(self):
        return {
            "command": "usr/local/bin/couchdb",
        }
        
    def create_database(self, name):
        import couchdb
        server = couchdb.Server("http://127.0.0.1:5984/")
        if name in server:
            debug("couchdb database already present: " + name)
        else:
            debug("creating couchdb database: " + name)
            server.create(name)
            
    def add_design_doc(self, dbname, path):
        """Adds a design document from the given path relative to couchapps/ direcotory to a couchdb database.
        """
        debug("adding design doc from %s to %s database" % (path, dbname))
        
        import couchdb
        server = couchdb.Server("http://127.0.0.1:5984/")
        db = server[dbname]
        
        from openlibrary.core.lists.tests.test_updater import read_couchapp
        design_doc = read_couchapp(path)
        id = design_doc['_id']
        if id in db:
            design_doc['_rev'] = db[id]['_rev']
        db[id] = design_doc
        db.commit()
    
class Infobase(Process):
    def get_specs(self):
        return {
            "command": INTERP + " ./scripts/infobase-server conf/infobase.yml 7500"
        }
        
    def get(self, path):
        try:
            response = urllib2.urlopen("http://127.0.0.1:7500/openlibrary.org" + path)
            return response.read()
        except urllib2.HTTPError, e:
            if e.getcode() == 404:
                return None
            else:
                raise
        
    def get_doc(self, key):
        import simplejson
        json = self.get("/get?key=" + key)
        return json and simplejson.loads(json)
        
    def post(self, path, data):   
        if isinstance(data, dict):
            data = urllib.urlencode(data)
        return urllib2.urlopen("http://127.0.0.1:7500/openlibrary.org" + path, data).read()
        
class DBTask:
    def getstatusoutput(self, cmd):
        debug("Executing", cmd)
        status, output = commands.getstatusoutput(cmd)
        debug(output)
        return status, output
        
    def has_database(self, name):
        status, output = self.getstatusoutput("psql %s -c 'select 1'" % name)
        return status == 0
        
    def has_table(self, db, column):
        status, output = self.getstatusoutput("psql %s -c '\d %s'" % (db, column))
        return status == 0
        
    def create_database(self, name):
        if self.has_database(name):
            debug("%s database is already present" % name)
        else:
            debug("creating %s database" % name)
            system("createdb " + name)

## Tasks

class setup_virtualenv:
    """Creates a new virtualenv and exec this script using python from that
    virtual env.
    """
    def run(self):
        global INTERP
    
        pyenv = os.path.expanduser(config['virtualenv'])
        INTERP = pyenv + "/bin/python"
    
        if sys.executable != INTERP:
            info("creating virtualenv at", pyenv)
            system("virtualenv " + pyenv)
        
            info("restarting the script with python from", INTERP)
            env = dict(os.environ)
            env['PATH'] = pyenv + "/bin:usr/local/bin:" + env['PATH']
            env['LD_LIBRARY_PATH'] = 'usr/local/lib'
            env['DYLD_LIBRARY_PATH'] = 'usr/local/lib'
            os.execvpe(INTERP, [INTERP] + sys.argv, env)
        
class install_python_dependencies:
    def run(self):
        info("installing python dependencies")
        system(INTERP + " setup.py develop")
    
class install_solr:
    def run(self):
        info("installing solr...")
    
        download_and_extract("http://www.archive.org/download/ol_vendor/apache-solr-1.4.0.tgz")
        
        base  = CWD.join("usr/local/apache-solr-1.4.0")
        solr = CWD.join("usr/local/solr")

        types = 'authors', 'editions', 'works', 'subjects', 'inside'
        for t in types:
            solr.mkdir("solr", t)
            
        for f in "etc lib logs webapps start.jar".split():
            src = base.join("example", f)
            dest = solr.join(f)
            src.copy_to(dest, recursive=True)
            
        CWD.join("conf/solr-biblio/solr.xml").copy_to(solr.join("solr"))

        solrconfig = base.join("example/solr/conf/solrconfig.xml").read()
    
        for t in types:
            if not solr.join("solr", t, "conf").exists():
                base.join("example/solr/conf").copy_to(solr.join("solr", t, "conf"), recursive=True)
                
            CWD.join("conf/solr-biblio", t + ".xml").copy_to(solr.join("solr", t, "conf/schema.xml"))
            
            f = solr.join("solr", t, "conf/solrconfig.xml")
            debug("creating", f.path)
            f.write(solrconfig.replace("./solr/data", "./solr/%s/data" % t))

class install_couchdb_lucene:
    def run(self):    
        info("installing couchdb lucene...")
        download_and_extract("http://www.archive.org/download/ol_vendor/couchdb-lucene-0.6-SNAPSHOT-dist.tar.gz")
        os.system("cd usr/local/etc && ln -fs ../couchdb-lucene-0.6-SNAPSHOT/conf couchdb-lucene")

class checkout_submodules:
    def run(self):
        info("checking out git submodules ...")
        system("git submodule init")
        system("git submodule update")

class install_couchdb:
    """Installs couchdb and updates configuration files..
    """
    def run(self):
        info("installing couchdb ...")
        distro = find_distro()
        if distro == "osx":
            self.install_osx()
        else:
            self.install_linux()
        self.copy_config_files()    
            
    def copy_config_files(self):
        debug("copying config files")
        CWD.join("conf/couchdb/local.ini").copy_to("usr/local/etc/couchdb/")
        
    def install_osx(self):
        download_url = "http://www.archive.org/download/ol_vendor/couchdb-1.0.1-osx-binaries.tgz"
        
        download_and_extract(download_url, dirname="couchdb_1.0.1")
        # mac os x distribution uses relative paths. So no need to fix files.
        
        os.system("cd usr/local/etc && ln -sf ../couchdb_1.0.1/etc/couchdb .")
        
    def install_linux(self):
        download_url = "http://www.archive.org/download/ol_vendor/couchdb-1.0.1-linux-binaries.tgz"
        download_and_extract(download_url, dirname="couchdb-1.0.1")
        self.fix_linux_paths()
        
        os.system("cd usr/local/bin && ln -fs ../couchdb-1.0.1/bin/couchdb .")
        os.system("cd usr/local/etc && ln -sf ../couchdb-1.0.1/etc/couchdb .")
        
    def fix_linux_paths(self):
        root = CWD.join("usr/local/couchdb-1.0.1")
        DEFAULT_ROOT = "/home/anand/couchdb-1.0.1"
        
        for f in "bin/couchdb bin/couchjs bin/erl etc/couchdb/default.ini etc/init.d/couchdb etc/logrotate.d/couchdb lib/couchdb/erlang/lib/couch-1.0.1/ebin/couch.app".split():
    	    debug("fixing paths in", f)
            f = root.join(f)
            f.write(f.read().replace(DEFAULT_ROOT, root.path))
                
class install_postgresql:
    """Installs postgresql on Mac OS X.
    Doesn't do anything on Linux.
    """
    def run(self):
        distro = find_distro()
        if distro == "osx":
            self.install()
        
    def install(self):
        info("installing postgresql..")
        download_url = "http://www.archive.org/download/ol_vendor/postgresql-8.4.4-osx-binaries.tgz"
        download_and_extract(download_url, dirname="postgresql-8.4.4")
        system("cd usr/local/bin && ln -fs ../postgresql-8.4.4/bin/* .")
        system("cd usr/local/lib && ln -fs ../postgresql-8.4.4/lib/* .")
        
        if not os.path.exists("var/lib/postgresql/base"):
            system("usr/local/bin/initdb --no-locale var/lib/postgresql")
        
class start_postgresql:
    def run(self):
        distro = find_distro()
        if distro == "osx":
            self.start()
        
    def start(self):
        info("starting postgresql..")
        
        pg = Postgres()
        register_cleanup(pg.stop)
        pg.start()
        
        
class setup_coverstore(DBTask):
    """Creates and initialized coverstore db."""
    def run(self):
        info("setting up coverstore database")
        self.create_database("coverstore")
        
        if self.has_table("coverstore", "cover"):
            debug("schema is already loaded")
        else:
            debug("loading schema")
            system("psql coverstore < openlibrary/coverstore/schema.sql")
        
class setup_ol(DBTask):
    def run(self):
        info("setting up openlibrary database")
        self.create_database("openlibrary")
        
        Infobase().run_tasks(self.ol_install)
        self.create_ebook_count_db()
        
    def ol_install(self):
        info("    running OL setup script")
        system(INTERP + " ./scripts/openlibrary-server conf/openlibrary.yml install")
        
    def create_ebook_count_db(self):
        info("    setting up openlibrary_ebook_count database")
        self.create_database("openlibrary_ebook_count")
        
        schema = """
        create table subjects (
    		field text not null,
    		key character varying(255),
    		publish_year integer, 
    		ebook_count integer,
    		PRIMARY KEY (field, key, publish_year)
    	);
    	CREATE INDEX field_key ON subjects(field, key);
        """
        
        if not self.has_table("openlibrary_ebook_count", "subjects"):
            import web
            db = web.database(dbn="postgres", db="openlibrary_ebook_count", user=os.getenv("USER"), pw="")
            db.printing = False
            db.query(schema)    	

class setup_couchdb:
    """Creates couchdb databases required for OL and adds design documents to them."""
    def run(self):
        info("setting up couchdb")
        self.couchdb = CouchDB()
        self.couchdb.run_tasks(self.create_dbs, self.add_design_docs)
        
    def create_dbs(self):
        info("    creating databases")
        self.couchdb.create_database("works")
        self.couchdb.create_database("editions")
        self.couchdb.create_database("seeds")
        self.couchdb.create_database("admin")
        
        
    def add_design_docs(self):
        info("    adding design docs")
        self.couchdb.add_design_doc("works", "works/seeds")
        self.couchdb.add_design_doc("editions", "editions/seeds")
        self.couchdb.add_design_doc("seeds", "seeds/dirty")
        self.couchdb.add_design_doc("seeds", "seeds/sort")

class setup_accounts:
    """Task for creating openlibrary account and adding it to admin and api usergroups.
    """
    def run(self):
        info("setting up accounts...")
        self.infobase = Infobase()
        self.infobase.run_tasks(self.create_account, self.add_to_usergroups)
        
    def create_account(self):
        infobase = self.infobase
        if not infobase.get_doc("/people/openlibrary"):
            info("    creating openlibrary account")
            infobase.post("/account/register", {
                "username": "openlibrary",
                "password": "openlibrary",
                "email": "openlibrary@example.com",
                "displayname": "Open Library"
            })
        else:
            info("    openlibrary account is already present")
            
        debug("marking openlibrary as verified user...")
        infobase.post("/account/update_user_details", {
            "username": "openlibrary",
            "verified": "true"
        })
        
    def add_to_usergroups(self):
        infobase = self.infobase
        info("    adding openlibrary to admin and api usergroups")
        usergroups = [
            {
                "key": "/usergroup/api",
                "type": {"key": "/type/usergroup"},
                "members": [
                    {"key": "/people/admin"},
                    {"key": "/people/openlibrary"},
                ]
            }, 
            {
                "key": "/usergroup/api",
                "type": {"key": "/type/usergroup"},
                "members": [
                    {"key": "/people/admin"},
                    {"key": "/people/openlibrary"},
                ]
            }
        ]
        import simplejson
        infobase.post("/save_many", {
            "query": simplejson.dumps(usergroups),
            "comment": "Added openlibrary to admin and api usergroups."
        })


cleanup_tasks = []

def register_cleanup(cleanup):
    cleanup_tasks.append(cleanup)
        
def main():
    setup_dirs()
    global config
    config = read_config()
    
    tasks = [
        setup_virtualenv(),
        install_python_dependencies(),
    
        checkout_submodules(),
    
        install_couchdb(),
        install_solr(),
        install_couchdb_lucene(),
        install_postgresql(),
        
        start_postgresql(),        
        
        setup_coverstore(),
        setup_ol(),
        setup_couchdb(),
        setup_accounts()
    ]

    try:
        for task in tasks:
            task.run()
    finally:
        for cleanup in cleanup_tasks:
            cleanup()
    
if __name__ == '__main__':
    main()