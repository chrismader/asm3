#!/usr/bin/python

import al
import audit
import base64
import configuration
import db
import mimetypes
import os, sys
import smcom
import utils
import web
from sitedefs import DBFS_STORE, DBFS_FILESTORAGE_FOLDER, DBFS_S3_BUCKET

class DBFSStorage(object):
    """ DBFSStorage factory """
    o = None
    def __init__(self, dbo, url = "default" ):
        """ Creates the correct storage object from mode or url """
        if url == "default":
            self._storage_from_mode(dbo)
        else:
            self._storage_from_url(dbo, url)

    def _storage_from_url(self, dbo, url):
        """ Creates an appropriate storage object for the url given. """
        if url is None or url == "" or url.startswith("base64:"):
            self.o = B64DBStorage(dbo)
        elif url.startswith("file:"):
            self.o = FileStorage(dbo)
        else:
            raise DBFSError("Invalid storage URL: %s" % url)

    def _storage_from_mode(self, dbo):
        """ Creates an appropriate storage object for the mode given """
        if DBFS_STORE == "database":
            self.o = B64DBStorage(dbo)
        elif DBFS_STORE == "file":
            self.o = FileStorage(dbo)
        else:
            raise DBFSError("Invalid storage mode: %s" % DBFS_STORE)

    def _extension_from_filename(self, filename):
        if filename is None or filename.find(".") == -1: return ""
        return filename[filename.rfind("."):]

    def get(self, dbfsid, url):
        """ Get file data for dbfsid/url """
        return self.o.get(dbfsid, url)
    def put(self, dbfsid, filename, filedata):
        """ Store filedata for dbfsid, returning a url """
        return self.o.put(dbfsid, filename, filedata)
    def delete(self, url):
        """ Delete filedata for url """
        return self.o.delete(url)
    def url_prefix(self):
        return self.o.url_prefix()

class B64DBStorage(DBFSStorage):
    """ Storage class for base64 encoding media and storing them
        in the database """
    dbo = None
    
    def __init__(self, dbo):
        self.dbo = dbo
    
    def get(self, dbfsid, dummy):
        """ Returns the file data for dbfsid or blank if not found/error """
        r = db.query_tuple(self.dbo, "SELECT Content FROM dbfs WHERE ID = '%d'" % dbfsid)
        if len(r) == 0:
            raise DBFSError("Could not find content for ID %s" % dbfsid)
        try:
            return base64.b64decode(r[0][0])
        except:
            em = str(sys.exc_info()[0])
            raise DBFSError("Failed unpacking base64 content with ID %s: %s" % (dbfsid, em))

    def put(self, dbfsid, filename, filedata):
        """ Stores the file data and returns a URL """
        url = "base64:"
        s = base64.b64encode(filedata)
        db.execute(self.dbo, "UPDATE dbfs SET URL = '%s', Content = '%s' WHERE ID = %d" % (url, s, dbfsid))
        return url

    def delete(self, url):
        """ Do nothing - removing the database row takes care of it """
        pass

    def url_prefix(self):
        return "base64:"

class FileStorage(DBFSStorage):
    """ Storage class for putting media on disk """
    dbo = None
    
    def __init__(self, dbo):
        self.dbo = dbo

    def get(self, dbfsid, url):
        """ Returns the file data for url """
        filepath = "%s/%s/%s" % (DBFS_FILESTORAGE_FOLDER, self.dbo.database, url.replace("file:", ""))
        f = open(filepath, "rb")
        s = f.read()
        f.close()
        return s

    def put(self, dbfsid, filename, filedata):
        """ Stores the file data (clearing the Content column) and returns the URL """
        try:
            path = "%s/%s" % (DBFS_FILESTORAGE_FOLDER, self.dbo.database)
            os.mkdir(path)
        except OSError:
            pass # Directory already exists - ignore
        extension = self._extension_from_filename(filename)
        filepath = "%s/%s/%s%s" % (DBFS_FILESTORAGE_FOLDER, self.dbo.database, dbfsid, extension)
        url = "file:%s%s" % (dbfsid, extension)
        f = open(filepath, "wb")
        f.write(filedata)
        f.flush()
        f.close()
        os.chmod(filepath, 0o666) # Make the file world read/write
        db.execute(self.dbo, "UPDATE dbfs SET URL = '%s', Content = '' WHERE ID = %d" % (url, dbfsid))
        return url

    def delete(self, url):
        """ Deletes the file data """
        filepath = "%s/%s/%s" % (DBFS_FILESTORAGE_FOLDER, self.dbo.database, url.replace("file:", ""))
        try:
            os.unlink(filepath)
        except Exception as err:
            al.error("Failed deleting '%s': %s" % (url, err), "FileStorage.delete", self.dbo)

    def url_prefix(self):
        return "file:"

class S3Storage(DBFSStorage):
    """ Storage class for putting media in Amazon S3 """
    dbo = None
    
    def __init__(self, dbo):
        self.dbo = dbo

    def get(self, dbfsid, url):
        """ Returns the file data for url """
        name = url.replace("s3:", "")
        remotepath = "s3://%s/%s/%s" % (DBFS_S3_BUCKET, self.dbo.database, name)
        localpath = "/tmp/%s" % name
        returncode, output = utils.cmd("aws s3 cp %s %s" % (remotepath, localpath))
        if returncode == 0:
            f = open(localpath, "rb")
            s = f.read()
            f.close()
            os.unlink(localpath)
            return s
        raise DBFSError("Failed retrieving from S3: %s %s" % (returncode, output))

    def put(self, dbfsid, filename, filedata):
        """ Stores the file data (clearing the Content column) and returns the URL """
        # S3 does not require folders to be created before upload as they are part of the filename
        extension = self._extension_from_filename(filename)
        remotepath = "s3://%s/%s/%s%s" % (DBFS_S3_BUCKET, self.dbo.database, dbfsid, extension)
        localpath = "/tmp/%s%s" % (dbfsid, extension)
        url = "s3:%s%s" % (dbfsid, extension)
        f = open(localpath, "wb")
        f.write(filedata)
        f.flush()
        f.close()
        returncode, output = utils.cmd("aws s3 cp %s %s" % (localpath, remotepath))
        if returncode == 0:
            db.execute(self.dbo, "UPDATE dbfs SET URL = '%s', Content = '' WHERE ID = %d" % (url, dbfsid))
            os.unlink(localpath)
            return url
        raise DBFSError("Failed storing in S3: %s %s" % (returncode, output))

    def delete(self, url):
        """ Deletes the file data """
        remotepath = "s3://%s/%s/%s" % (DBFS_S3_BUCKET, self.dbo.database, url.replace("s3:", ""))
        returncode, output = utils.cmd("aws s3 rm %s" % remotepath)
        if returncode != 0:
            raise DBFSError("Failed deleting from S3: %s %s" % (returncode, output))

    def url_prefix(self):
        return "s3:"

class DBFSError(web.HTTPError):
    """ Custom error thrown by dbfs modules """
    def __init__(self, msg):
        status = '500 Internal Server Error'
        headers = { 'Content-Type': "text/html" }
        data = "<h1>DBFS Error</h1><p>%s</p>" % msg
        web.HTTPError.__init__(self, status, headers, data)

def create_path(dbo, path, name):
    """ Creates a new DBFS folder """
    db.execute(dbo, "INSERT INTO dbfs (ID, Name, Path) VALUES (%d, '%s', '%s')" % (db.get_id(dbo, "dbfs"), name, path))

def check_create_path(dbo, path):
    """ Verifies that portions of a path exist and creates them if not
    only goes to two levels deep as we never need more than that
    for anything within ASM.
    """
    def check(name, path):
        if 0 == db.query_int(dbo, "SELECT COUNT(*) FROM dbfs WHERE Name = '%s' AND Path = '%s'" % (name, path)):
            create_path(dbo, path, name)
    pat = path[1:].split("/")
    check(pat[0], "/")
    if len(pat) > 1:
        check(pat[1], "/" + pat[0])

def get_string_filepath(dbo, filepath):
    """
    Gets DBFS file contents as a string. Returns
    an empty string if the file is not found. Splits
    filepath into the name and path to do it.
    """
    name = filepath[filepath.rfind("/")+1:]
    path = filepath[0:filepath.rfind("/")]
    return get_string(dbo, name, path)

def get_string(dbo, name, path = ""):
    """
    Gets DBFS file contents as a string.
    If no path is supplied, just finds the first file with that name
    in the dbfs (useful for media files, which have unique names)
    """
    if path != "":
        path = " AND Path = %s" % db.ds(path)
    r = db.query(dbo, "SELECT ID, URL FROM dbfs WHERE Name =%s%s" % (db.ds(name), path))
    if len(r) == 0:
        return "" # compatibility with old behaviour - relied on by publishers
        #raise DBFSError("No element found for path=%s, name=%s" % (path, name))
    r = r[0]
    o = DBFSStorage(dbo, r["URL"])
    return o.get(r["ID"], r["URL"])

def get_string_id(dbo, dbfsid):
    """
    Gets DBFS file contents as a string. Returns
    an empty string if the file is not found.
    """
    r = db.query(dbo, "SELECT URL FROM dbfs WHERE ID=%s" % dbfsid)
    if len(r) == 0:
        return "" # compatibility with old behaviour - relied on by publishers
        #raise DBFSError("No row found with ID %s" % dbfsid)
    r = r[0]
    o = DBFSStorage(dbo, r["URL"])
    return o.get(dbfsid, r["URL"])

def rename_file(dbo, path, oldname, newname):
    """
    Renames a file in the dbfs.
    """
    db.execute(dbo, "UPDATE dbfs SET Name = '%s' WHERE Name = '%s' AND " \
        "Path = '%s'" % \
        (newname, oldname, path))

def rename_file_id(dbo, dbfsid, newname):
    """
    Renames a file in the dbfs.
    """
    db.execute(dbo, "UPDATE dbfs SET Name = '%s' WHERE ID = %d" % (newname, dbfsid))

def put_file(dbo, name, path, filepath):
    """
    Reads the the file from filepath and stores it with name/path
    """
    check_create_path(dbo, path)
    f = open(filepath, "rb")
    s = f.read()
    f.close()
    dbfsid = db.get_id(dbo, "dbfs")
    db.execute(dbo, "INSERT INTO dbfs (ID, Name, Path) VALUES (%d, '%s', '%s')" % ( dbfsid, name, path ))
    o = DBFSStorage(dbo)
    o.put(dbfsid, name, s)
    return dbfsid

def put_string(dbo, name, path, contents):
    """
    Stores the file contents at the name and path. If the file exists, overwrites it.
    """
    check_create_path(dbo, path)
    name = name.replace("'", "")
    path = path.replace("'", "")
    dbfsid = db.query_int(dbo, "SELECT ID FROM dbfs WHERE Path = '%s' AND Name = '%s'" % (path, name))
    if dbfsid == 0:
        dbfsid = db.get_id(dbo, "dbfs")
        db.execute(dbo, "INSERT INTO dbfs (ID, Name, Path) VALUES (%d, '%s', '%s')" % ( dbfsid, name, path ))
    o = DBFSStorage(dbo)
    o.put(dbfsid, name, contents)
    return dbfsid

def put_string_id(dbo, dbfsid, name, contents):
    """
    Stores the file contents at the id given.
    """
    o = DBFSStorage(dbo)
    o.put(dbfsid, name, contents)
    return dbfsid

def put_string_filepath(dbo, filepath, contents):
    """
    Stores the file contents at the name/path given.
    """
    name = filepath[filepath.rfind("/")+1:]
    path = filepath[0:filepath.rfind("/")]
    return put_string(dbo, name, path, contents)

def replace_string(dbo, content, name, path = ""):
    """
    Replaces the file contents given as a string in the dbfs
    with the name and path given. If no path is given, looks it
    up by just the name.
    """
    if path != "":
        path = " AND Path = %s" % db.ds(path)
    r = db.query(dbo, "SELECT ID, URL, Name FROM dbfs WHERE Name =%s%s" % (db.ds(name), path))
    if len(r) == 0:
        raise DBFSError("No item found for path=%s, name=%s" % (path, name))
    r = r[0]
    o = DBFSStorage(dbo, r["URL"])
    o.put(r["ID"], r["NAME"], content)
    return r["ID"]

def get_file(dbo, name, path, saveto):
    """
    Gets DBFS file contents and saves them to the
    filename given. Returns True for success
    """
    s = get_string(dbo, name, path)
    f = open(saveto, "wb")
    f.write(s)
    f.close()
    return True

def file_exists(dbo, name):
    """
    Return True if a file with name exists in the database.
    """
    return db.query_int(dbo, "SELECT COUNT(*) FROM dbfs WHERE Name = '%s'" % name) > 0

def get_files(dbo, name, path, saveto):
    """
    Gets DBFS files for the pattern given in name (use % like db)
    and belonging to path (blank for all paths). saveto is
    the folder to save all the files to. Returns True for success
    """
    if path != "":
        path = " AND Path = '%s'" % path
    rows = db.query(dbo, "SELECT ID, URL FROM dbfs WHERE LOWER(Name) LIKE '%s'%s" % (name, path))
    if len(rows) > 0:
        for r in rows:
            o = DBFSStorage(dbo, r["URL"])
            f = open(saveto, "wb")
            f.write(o.get(r["ID"], r["URL"]))
            f.close()
        return True
    return False

def delete_path(dbo, path):
    """
    Deletes all items matching the path given
    """
    rows = db.query(dbo, "SELECT ID, URL FROM dbfs WHERE Path LIKE '%s'" % path)
    db.execute(dbo, "DELETE FROM dbfs WHERE Path LIKE '%s'" % path)
    for r in rows:
        o = DBFSStorage(dbo, r["URL"])
        o.delete(r["URL"])

def delete(dbo, name, path = ""):
    """
    Deletes all items matching the name and path given
    """
    if path != "":
        path = " AND Path = %s" % db.ds(path)
    rows = db.query(dbo, "SELECT ID, URL FROM dbfs WHERE Name=%s%s" % (db.ds(name), path))
    db.execute(dbo, "DELETE FROM dbfs WHERE Name=%s%s" % (db.ds(name), path))
    for r in rows:
        o = DBFSStorage(dbo, r["URL"])
        o.delete(r["URL"])

def delete_filepath(dbo, filepath):
    """
    Deletes the dbfs entry for the filepath
    """
    name = filepath[filepath.rfind("/")+1:]
    path = filepath[0:filepath.rfind("/")]
    delete(dbo, name, path)

def delete_id(dbo, dbfsid):
    """
    Deletes the dbfs entry for the id
    """
    url = db.query_string(dbo, "SELECT URL FROM dbfs WHERE ID=%d" % dbfsid)
    db.execute(dbo, "DELETE FROM dbfs WHERE ID = %d" % dbfsid)
    o = DBFSStorage(dbo, url)
    o.delete(url)

def list_contents(dbo, path):
    """
    Returns a list of items in the path given. Directories
    are identifiable by not having a file extension.
    """
    rows = db.query(dbo, "SELECT Name FROM dbfs WHERE Path = '%s'" % path)
    l = []
    for r in rows:
        l.append(r["NAME"])
    return l

# End of storage primitives

def get_nopic(dbo):
    """
    Returns the nopic jpeg file
    """
    return get_string(dbo, "nopic.jpg", "/reports")

def get_html_publisher_templates(dbo):
    """
    Returns a list of available template/styles for the html publisher
    """
    l = []
    rows = db.query(dbo, "SELECT Name, Path FROM dbfs WHERE Path Like '/internet' ORDER BY Name")
    hasRootStyle = False
    for r in rows:
        if r["NAME"].find(".dat") != -1:
            hasRootStyle = True
        elif r["NAME"].find(".") == -1:
            l.append(r["NAME"])
    if hasRootStyle:
        l.append(".")
    return sorted(l)

def get_html_publisher_templates_files(dbo):
    """
    Returns a list of all templates/styles with their header, footer and bodies.
    This call can only be used for grabbing them for the UI. It turns < and > into
    &lt; and &gt; so that json encoding the result doesn't blow up the browser.
    """
    templates = []
    available = get_html_publisher_templates(dbo)
    for name in available:
        if name != ".":
            head = get_string(dbo, "head.html", "/internet/%s" % name)
            if head == "": head = get_string(dbo, "pih.dat", "/internet/%s" % name)
            body = get_string(dbo, "body.html", "/internet/%s" % name)
            if body == "": body = get_string(dbo, "pib.dat", "/internet/%s" % name)
            foot = get_string(dbo, "foot.html", "/internet/%s" % name)
            if foot == "": foot = get_string(dbo, "pif.dat", "/internet/%s" % name)
            templates.append({ "NAME": name, "HEADER": head, "BODY": body, "FOOTER": foot})
    return templates

def update_html_publisher_template(dbo, username, name, header, body, footer):
    """
    Creates a new html publisher template with the name given. If the
    template already exists, it recreates it.
    """
    delete_path(dbo, "/internet/" + name)
    delete(dbo, name, "/internet")
    create_path(dbo, "/internet", name)
    put_string(dbo, "head.html", "/internet/%s" % name, header)
    put_string(dbo, "body.html", "/internet/%s" % name, body)
    put_string(dbo, "foot.html", "/internet/%s" % name, footer)
    al.debug("%s updated html template %s" % (username, name), "dbfs.update_html_publisher_template", dbo)
    audit.edit(dbo, username, "htmltemplate", 0, "altered html template '%s'" % name)

def delete_html_publisher_template(dbo, username, name):
    delete_path(dbo, "/internet/" + name)
    delete(dbo, name, "/internet")
    al.debug("%s deleted html template %s" % (username, name), "dbfs.update_html_publisher_template", dbo)
    audit.delete(dbo, username, "htmltemplate", 0, "remove html template '%s'" % name)

def sanitise_path(path):
    """ Strips disallowed chars from new paths """
    disallowed = (" ", "|", ",", "!", "\"", "'", "$", "%", "^", "*",
        "(", ")", "[", "]", "{", "}", "\\", ":", "@", "?", "+")
    for d in disallowed:
        path = path.replace(d, "_")
    return path

def get_document_templates(dbo):
    """
    Returns a combined list of document templates
    """
    templates = get_html_document_templates(dbo)
    if configuration.allow_odt_document_templates(dbo):
        templates += get_odt_document_templates(dbo)
    return templates

def get_html_document_templates(dbo):
    """
    Returns a list of all HTML document templates
    """
    return db.query(dbo, "SELECT ID, Name, Path FROM dbfs WHERE Name Like '%.html' AND Path Like '/templates%' ORDER BY Path, Name")

def get_odt_document_templates(dbo):
    """
    Returns a list of all ODT document templates
    """
    return db.query(dbo, "SELECT ID, Name, Path FROM dbfs WHERE Name Like '%.odt' AND Path Like '/templates%' ORDER BY Path, Name")

def create_document_template(dbo, username, name, ext = ".html", content = "<p></p>"):
    """
    Creates a document template from the name given.
    If there's no extension, adds it
    If it's a relative path (doesn't start with /) adds /templates/ to the front
    If it's an absolute path that doesn't start with /templates/, add /templates
    Changes spaces and unwanted punctuation to underscores
    """
    filepath = name
    if not filepath.endswith(ext): filepath += ext
    if not filepath.startswith("/"): filepath = "/templates/" + filepath
    if not filepath.startswith("/templates"): filepath = "/templates" + filepath
    filepath = sanitise_path(filepath)
    dbfsid = put_string_filepath(dbo, filepath, content)
    audit.create(dbo, username, "documenttemplate", dbfsid, "id: %d, name: %s" % (dbfsid, name))
    return dbfsid

def clone_document_template(dbo, username, dbfsid, newname):
    """
    Creates a new document template with the content from the dbfsid given.
    """
    # Get the extension/type from newname, defaulting to html
    ext = ".html"
    if newname.rfind(".") != -1:
        ext = newname[newname.rfind("."):]
    content = get_string_id(dbo, dbfsid)
    ndbfsid = create_document_template(dbo, username, newname, ext, content)
    audit.create(dbo, username, "documenttemplate", ndbfsid, "clone %d to %s (new id: %d)" % (dbfsid, newname, ndbfsid))
    return ndbfsid

def delete_document_template(dbo, username, dbfsid):
    """
    Deletes a document template. This is a separate function so auditing can be done.
    """
    delete_id(dbo, dbfsid)
    audit.delete(dbo, username, "documenttemplate", dbfsid, "delete template %d" % dbfsid)

def rename_document_template(dbo, username, dbfsid, newname):
    """
    Renames a document template.
    """
    if not newname.endswith(".html") and not newname.endswith(".odt"): newname += ".html"
    rename_file_id(dbo, dbfsid, newname)
    audit.edit(dbo, username, "documenttemplate", dbfsid, "rename %d to %s" % (dbfsid, newname))

def get_name_for_id(dbo, dbfsid):
    """
    Returns the filename of the item with id dbfsid
    """
    return db.query_string(dbo, "SELECT Name FROM dbfs WHERE ID = %d" % dbfsid)

def get_document_repository(dbo):
    """
    Returns a list of all documents in the /document_repository directory,
    also includes MIMETYPE field for display
    """
    rows = db.query(dbo, "SELECT ID, Name, Path FROM dbfs WHERE " \
        "Path Like '/document_repository%' AND Name Like '%.%' ORDER BY Path, Name")
    for r in rows:
        mimetype, encoding = mimetypes.guess_type("file://" + r["NAME"], strict=False)
        r["MIMETYPE"] = mimetype
    return rows

def get_report_images(dbo):
    """
    Returns a list of all extra images in the /reports directory
    """
    return db.query(dbo, "SELECT Name, Path FROM dbfs WHERE " \
        "(LOWER(Name) Like '%.jpg' OR LOWER(Name) Like '%.png' OR LOWER(Name) Like '%.gif') " \
        "AND Path Like '/report%' ORDER BY Path, Name")

def upload_report_image(dbo, fc):
    """
    Attaches an image from a form filechooser object and puts
    it in the /reports directory. 
    """
    ext = ""
    ext = fc.filename
    filename = utils.filename_only(fc.filename)
    filedata = fc.value
    ext = ext[ext.rfind("."):].lower()
    ispicture = ext == ".jpg" or ext == ".jpeg" or ext == ".png" or ext == ".gif"
    if not ispicture:
        raise utils.ASMValidationError("upload_report_image only accepts images.")
    put_string(dbo, filename, "/reports", filedata)

def upload_document_repository(dbo, path, filename, filedata):
    """
    Attaches a document from a form filechooser object and puts
    it in the /document_repository directory. 
    An extra path portion can be specified in path.
    """
    ext = ""
    ext = filename
    filename = utils.filename_only(filename)
    ext = ext[ext.rfind("."):].lower()
    if path != "" and path.startswith("/"): path = path[1:]
    if path == "":
        filepath = "/document_repository/%s" % filename
    else:
        path = sanitise_path(path)
        filepath = "/document_repository/%s/%s" % (path, filename)
    put_string_filepath(dbo, filepath, filedata)

def has_nopic(dbo):
    """
    Returns True if the database has a nopic.jpg file
    """
    return get_nopic(dbo) != ""

def has_html_document_templates(dbo):
    """
    Returns True if there are some html document templates in the database
    """
    return len(get_html_document_templates(dbo)) > 0

def switch_storage(dbo):
    """ Goes through all files in dbfs and swaps them into the current storage scheme """
    rows = db.query(dbo, "SELECT ID, Name, Path, URL FROM dbfs WHERE Name LIKE '%.%' ORDER BY ID")
    for i, r in enumerate(rows):
        al.debug("Storage transfer %s/%s (%d of %d)" % (r["PATH"], r["NAME"], i, len(rows)), "dbfs.switch_storage", dbo)
        source = DBFSStorage(dbo, r["URL"])
        target = DBFSStorage(dbo)
        # Don't bother if the file is already stored in the target format
        if source.url_prefix() == target.url_prefix():
            al.debug("source is already %s, skipping" % source.url_prefix(), "dbfs.switch_storage", dbo)
            continue
        try:
            filedata = source.get(r["ID"], r["URL"])
            target.put(r["ID"], r["NAME"], filedata)
        except Exception as err:
            al.error("Error reading, skipping: %s" % str(err), "dbfs.switch_storage", dbo)
    # smcom only - perform postgresql full vacuum after switching
    if smcom.active(): smcom.vacuum_full(dbo)

