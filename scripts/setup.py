#!/usr/bin/env python3
"""Connect HolyArrs, qBittorrent and Jellyfin on a fresh install. Standard library only.

Runs in the `setup` container: `docker compose run --rm setup seed|connect`.

seed     Before the first start: make the data folders and give qBittorrent a
         generated password.
connect  Once the apps answer: finish Jellyfin's first run, then make the
         HolyArrs owner and connect HolyArrs to qBittorrent, Jellyfin and two
         built-in torrent sites.

Each generated password or key is saved to credentials.json before it is used,
so a failed run can be resumed. An app this script finished is marked done and
never touched again; an app set up some other way is left as it is.
"""

import base64
import fcntl
import hashlib
import http.cookiejar
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APPDATA = Path("/appdata")
DATA = Path("/data")
HOST_CONFIG_DIR = os.environ.get("CONFIG_DIR", "/opt/appdata")  # for messages: the host's paths
HOST_DATA_DIR = os.environ.get("DATA_DIR", "/data")
PUID = int(os.environ.get("PUID", "1000"))
PGID = int(os.environ.get("PGID", "1000"))

QBITTORRENT = "http://qbittorrent:8080"
JELLYFIN = "http://jellyfin:8096"
HOLYARRS = "http://holyarrs:6262"

TIMEOUT = 30  # seconds per request
TEST_TIMEOUT = 60  # HolyArrs gives a Test up to 30 s; leave it room to answer
WAIT = 180  # seconds an app may take to become ready

FOLDERS = (("/data/media/movies", "movie", "Movies", "movies"), ("/data/media/tv", "show", "TV", "tvshows"))
SITES = (("knaben", "Knaben"), ("torrentscsv", "TorrentsCSV"))


class StepError(Exception):
    """A request failed; the message names the request and its answer."""


class Refused(StepError):
    """A saved password was refused: the app was changed by hand since."""


# --- credentials -----------------------------------------------------------

def setup_dir():
    return APPDATA / "stack-setup"


def creds_path():
    return setup_dir() / "credentials.json"


def load_creds():
    path = creds_path()
    if not path.exists():
        return {}
    try:
        creds = json.loads(path.read_text())
    except ValueError:
        raise StepError(f"{HOST_CONFIG_DIR}/stack-setup/credentials.json is not valid JSON; fix or remove it") from None
    if not isinstance(creds, dict):
        raise StepError(f"{HOST_CONFIG_DIR}/stack-setup/credentials.json is not a JSON object; fix or remove it")
    return creds


def private_dir():
    folder = setup_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def write_whole(path, text, owner=None):
    """Write path so that even a crash or power cut leaves the old file or the new one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if owner:
            os.chown(tmp, *owner)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def save_creds(creds):
    private_dir()
    write_whole(creds_path(), json.dumps(creds, indent=2))


def remember(creds, app, **values):
    """Save values for app before they are used, keeping any already saved."""
    entry = creds.setdefault(app, {})
    for key, value in values.items():
        entry.setdefault(key, value)
    save_creds(creds)
    return entry


def mark_done(creds, app):
    creds.setdefault(app, {})["done"] = True
    save_creds(creds)


def leave_alone(creds, app, name, todo):
    """Remember that app refused its saved password, so no later run signs in again.

    Repeated failed sign-ins lock accounts (Jellyfin) or ban the address (qBittorrent).
    """
    creds.setdefault(app, {})["left_alone"] = True
    save_creds(creds)
    print(f"{name}: the saved password was refused (changed by hand?); left as it is from now on. {todo}")


def skipped(saved, name):
    """True, with a line saying why, when an earlier run finished with this app."""
    if saved.get("done"):
        print(f"{name}: connected by an earlier run; left as it is")
    elif saved.get("left_alone"):
        print(f"{name}: refused its saved password in an earlier run; left as it is")
    else:
        return False
    return True


def new_password():
    return secrets.token_urlsafe(24)


# --- seed ------------------------------------------------------------------

def qbittorrent_conf_path():
    return APPDATA / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"


def password_hash(password, salt=None):
    """qBittorrent's Web UI password format: PBKDF2-SHA512, 100000 rounds."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return "@ByteArray(%s:%s)" % (base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def qbittorrent_conf(username, password, salt=None):
    # Ports come from the image's environment (WEBUI_PORT, TORRENTING_PORT).
    return (
        "[LegalNotice]\nAccepted=true\n\n"
        "[BitTorrent]\nSession\\DefaultSavePath=/data/downloads\n\n"
        "[WebUI]\nUsername=%s\nPassword_PBKDF2=\"%s\"\n" % (username, password_hash(password, salt))
    )


def make_owned_dirs(path):
    """mkdir -p, giving every folder it creates to PUID:PGID."""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for folder in reversed(missing):
        folder.mkdir()
        os.chown(folder, PUID, PGID)


def seed():
    for folder in ("downloads", "media/movies", "media/tv"):
        make_owned_dirs(DATA / folder)
    conf = qbittorrent_conf_path()
    if conf.exists():
        print("qBittorrent: already configured; its password stays as it is")
        return
    login = remember(load_creds(), "qbittorrent", username="admin", password=new_password())
    make_owned_dirs(conf.parent)
    write_whole(conf, qbittorrent_conf(login["username"], login["password"]), owner=(PUID, PGID))
    print("qBittorrent: password generated, downloads go to /data/downloads")


# --- HTTP ------------------------------------------------------------------

class Client:
    def __init__(self, base, host=None, headers=None):
        self.base = base
        self.headers = dict(headers or {})
        if host:
            self.headers["Host"] = host
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, form=False, timeout=TIMEOUT):
        headers = dict(self.headers)
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        except (urllib.error.URLError, OSError) as error:
            raise StepError(f"{method} {path}: {error}") from None
        try:
            return status, (json.loads(raw) if raw else None)
        except ValueError:
            return status, raw.decode(errors="replace")

    def ok(self, method, path, body=None, form=False, timeout=TIMEOUT):
        """The JSON object a 2xx answer carries; anything else is a StepError."""
        status, out = self.call(method, path, body, form, timeout)
        if status in (401, 403) and path.endswith(("/auth/login", "/AuthenticateByName")):
            raise Refused(f"the saved password was refused ({status})")
        if not 200 <= status < 300:
            raise StepError(f"{method} {path} answered {status}: {str(out)[:200]}")
        if out is not None and not isinstance(out, (dict, list)):
            raise StepError(f"{method} {path} answered {status} with something other than JSON: {str(out)[:120]}")
        return out

    def wait(self, path):
        """GET path until it answers 2xx JSON, or fail after WAIT seconds."""
        deadline = time.monotonic() + WAIT
        while True:
            try:
                return self.ok("GET", path)
            except StepError as error:
                last = error
            if time.monotonic() > deadline:
                raise StepError(f"not ready after {WAIT}s: {last}")
            time.sleep(3)


def listed(out, key, what):
    """The list HolyArrs or Jellyfin returns under key; a missing key is an error."""
    if not isinstance(out, dict) or not isinstance(out.get(key), list):
        raise StepError(f"unexpected answer listing {what}: no '{key}' list")
    return out[key]


# --- connect ---------------------------------------------------------------

def check_qbittorrent(creds):
    """The saved qBittorrent login if it still works, else None."""
    login = creds.get("qbittorrent")
    if not login:
        print("qBittorrent: set up before this script; HolyArrs's Downloader is left for you")
        return None
    if skipped(login, "qBittorrent"):
        return None
    qb = Client(QBITTORRENT)
    deadline = time.monotonic() + WAIT
    while True:
        try:
            status, out = qb.call("POST", "/api/v2/auth/login",
                                  {"username": login["username"], "password": login["password"]}, form=True)
            break
        except StepError:  # not answering yet; a wrong password is never retried
            if time.monotonic() > deadline:
                raise
            time.sleep(3)
    # Older versions answer 200 "Ok." or "Fails.", newer ones 204 or 401.
    answer = str(out).strip()
    if status == 204 or (status == 200 and answer == "Ok."):
        print("qBittorrent: signs in with the generated password")
        return login
    if status == 401 or (status == 200 and answer == "Fails."):
        leave_alone(creds, "qbittorrent", "qBittorrent", "Add it as a Downloader in HolyArrs' Settings yourself")
        return None
    raise StepError(f"qBittorrent sign-in answered {status}: {answer[:120]}")


def jellyfin_client(token=None):
    auth = 'MediaBrowser Client="media-stack", Device="setup", DeviceId="media-stack-setup", Version="1.0"'
    if token:
        auth += f', Token="{token}"'
    return Client(JELLYFIN, headers={"Authorization": auth})


def setup_jellyfin(creds):
    """Jellyfin's API key for HolyArrs, or None when Jellyfin is left as it is."""
    saved = creds.get("jellyfin", {})
    if skipped(saved, "Jellyfin"):
        return saved.get("api_key") if saved.get("done") else None
    jf = jellyfin_client()
    info = jf.wait("/System/Info/Public")
    if not info.get("StartupWizardCompleted"):
        jf.wait("/Startup/Configuration")  # answers 503 while first-start migrations run
        login = remember(creds, "jellyfin", username="admin", password=new_password())
        jf.ok("POST", "/Startup/Configuration", {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
        jf.ok("GET", "/Startup/User")
        jf.ok("POST", "/Startup/User", {"Name": login["username"], "Password": login["password"]})
        jf.ok("POST", "/Startup/Complete", {})
        print("Jellyfin: first run finished, admin created")
    elif "password" not in saved:
        print("Jellyfin: set up before this script; HolyArrs's media server is left for you")
        return None
    login = creds["jellyfin"]
    try:
        auth = jf.ok("POST", "/Users/AuthenticateByName", {"Username": login["username"], "Pw": login["password"]})
    except Refused:
        leave_alone(creds, "jellyfin", "Jellyfin", "Add it as a media server in HolyArrs' Settings yourself")
        return None
    jf = jellyfin_client(auth["AccessToken"])
    if not login.get("api_key"):
        keys = listed(jf.ok("GET", "/Auth/Keys"), "Items", "Jellyfin API keys")
        if not any(k.get("AppName") == "HolyArrs" for k in keys):
            jf.ok("POST", "/Auth/Keys?" + urllib.parse.urlencode({"app": "HolyArrs"}))
            keys = listed(jf.ok("GET", "/Auth/Keys"), "Items", "Jellyfin API keys")
        key = next((k.get("AccessToken") for k in keys if k.get("AppName") == "HolyArrs"), None)
        if not key:
            raise StepError("the API key for HolyArrs was not found after making it")
        login["api_key"] = key
        save_creds(creds)
    libraries = jf.ok("GET", "/Library/VirtualFolders")
    if not isinstance(libraries, list):
        raise StepError("unexpected answer listing Jellyfin libraries")
    for path, _, label, collection in FOLDERS:
        if not any(path in lib.get("Locations", []) for lib in libraries):
            query = urllib.parse.urlencode({"name": label, "collectionType": collection, "refreshLibrary": "false"})
            jf.ok("POST", "/Library/VirtualFolders?" + query, {"LibraryOptions": {"PathInfos": [{"Path": path}]}})
            print(f"Jellyfin: library {label} added ({path})")
    mark_done(creds, "jellyfin")
    return login["api_key"]


def passes_test(ha, path, ident):
    """(passed, why) for HolyArrs' own Test of one item."""
    try:
        status, out = ha.call("POST", f"{path}/{ident}/test", timeout=TEST_TIMEOUT)
    except StepError as error:
        return False, str(error)
    passed = 200 <= status < 300 and (not isinstance(out, dict) or out.get("ok", True) is not False)
    return passed, f"{status}: {str(out)[:160]}"


def add_once(ha, path, key, what, match, body):
    """The id of the item in HolyArrs that match() picks, adding body if none."""
    found = next((item for item in listed(ha.ok("GET", path), key, what) if match(item)), None)
    if found is None:
        found = ha.ok("POST", path, body)
        print(f"HolyArrs: {what} added")
    if not isinstance(found, dict) or "id" not in found:
        raise StepError(f"unexpected answer adding {what}: no id")
    return found["id"]


def setup_holyarrs(creds, jellyfin_key, jellyfin_failed=False):
    saved = creds.get("holyarrs", {})
    if skipped(saved, "HolyArrs"):
        return
    # Host: localhost is always accepted and saves nothing, with or without a proxy.
    ha = Client(HOLYARRS, host="localhost")
    state = ha.wait("/api/v1/auth/state")
    if state.get("needs_setup"):
        login = remember(creds, "holyarrs", username="admin", password=new_password())
        me = ha.ok("POST", "/api/v1/auth/setup", {"name": login["username"], "password": login["password"]})
        print("HolyArrs: owner created")
    elif "password" in saved:
        try:
            me = ha.ok("POST", "/api/v1/auth/login", {"name": saved["username"], "password": saved["password"]})
        except Refused:
            leave_alone(creds, "holyarrs", "HolyArrs",
                        "Add its library folders, Downloader, media server and sources in its Settings yourself")
            return
    else:
        print("HolyArrs: has an owner this script did not make; left as it is")
        return
    if not isinstance(me, dict) or not me.get("csrf"):
        raise StepError("signing in did not return a session (is two-factor sign-in on?)")
    ha.headers["X-CSRF-Token"] = me["csrf"]
    problems = []
    # Only now, with HolyArrs known to be ours: a refused qBittorrent sign-in
    # counts towards its ban, so it is not spent on a HolyArrs left alone.
    try:
        qbit_login = check_qbittorrent(creds)
    except StepError as error:
        qbit_login = None
        problems.append(f"qBittorrent did not answer as expected ({error})")

    for path, kind, label, _ in FOLDERS:
        add_once(ha, "/api/v1/library-folders", "library_folders", f"library folder {path}",
                 lambda f, path=path: f.get("path") == path, {"path": path, "kind": kind, "label": label})

    local = []
    if qbit_login:
        local.append(("qBittorrent", "/api/v1/downloaders", add_once(
            ha, "/api/v1/downloaders", "downloaders", "Downloader qBittorrent",
            lambda d: d.get("kind") == "qbittorrent" and d.get("url") == QBITTORRENT,
            {"name": "qBittorrent", "kind": "qbittorrent", "url": QBITTORRENT, "username": qbit_login["username"],
             "secret": qbit_login["password"], "category": "holyarrs", "allow_local_network": True})))
    elif not problems:
        print("HolyArrs: Downloader skipped (no working qBittorrent password)")
    if jellyfin_key:
        local.append(("Jellyfin", "/api/v1/media-servers", add_once(
            ha, "/api/v1/media-servers", "media_servers", "media server Jellyfin",
            lambda m: m.get("kind") == "jellyfin" and m.get("url") == JELLYFIN,
            {"name": "Jellyfin", "kind": "jellyfin", "url": JELLYFIN, "token": jellyfin_key, "enabled": True,
             "on_import": True, "on_rename": True, "on_delete": True, "allow_local_network": True})))
    elif jellyfin_failed:
        problems.append("the media server waits for the Jellyfin failure above to be fixed")
    else:
        print("HolyArrs: media server skipped (no Jellyfin key)")
    for name, path, ident in local:
        passed, why = passes_test(ha, path, ident)
        if passed:
            print(f"HolyArrs: {name} passes Test")
        else:
            problems.append(f"{name} was added but fails HolyArrs' Test ({why})")

    for site, name in SITES:
        ident = add_once(ha, "/api/v1/sources", "sources", f"source {name}",
                         lambda s, site=site: s.get("site") == site, {"name": name, "format": "site", "site": site})
        passed, why = passes_test(ha, "/api/v1/sources", ident)
        # An outside site being down is not this install's fault: warn only.
        print(f"HolyArrs: {name} passes Test" if passed else f"warn: HolyArrs: {name} does not answer right now ({why})")

    status, out = ha.call("POST", "/api/v1/auth/setup/done")
    if not 200 <= status < 300:
        problems.append(f"could not mark the first run as finished ({status}: {str(out)[:120]})")
    if problems:
        raise StepError("; ".join(problems))
    mark_done(creds, "holyarrs")


def connect():
    creds = load_creds()
    failed = []

    def step(name, action):
        try:
            return action()
        except Exception as error:  # one app's trouble must not stop the others
            print(f"FAIL {name}: {type(error).__name__}: {error}", file=sys.stderr)
            failed.append(name)
            return None

    jellyfin_key = step("Jellyfin", lambda: setup_jellyfin(creds))
    step("HolyArrs", lambda: setup_holyarrs(creds, jellyfin_key, jellyfin_failed=bool(failed)))
    if creds_path().exists():
        print(f"Passwords and keys: {HOST_CONFIG_DIR}/stack-setup/credentials.json (root only)")
    if failed:
        print("Fix the failure above and run `make up` again; finished steps are kept.", file=sys.stderr)
    return 1 if failed else 0


def host_path(path):
    """The host's name for a path inside this container."""
    for inside, outside in ((str(APPDATA), HOST_CONFIG_DIR), (str(DATA), HOST_DATA_DIR)):
        if path == inside or path.startswith(inside + "/"):
            return outside + path[len(inside):]
    return path


def main(argv):
    if argv[1:] not in (["seed"], ["connect"]):
        print("usage: setup.py seed|connect", file=sys.stderr)
        return 2
    try:
        # One run at a time: two overlapping runs would race on credentials.json.
        with open(private_dir() / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if argv[1] == "seed":
                seed()
                return 0
            return connect()
    except StepError as error:
        print(f"FAIL setup: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        where = host_path(str(error.filename)) if error.filename else "a file"
        print(f"FAIL setup: {where}: {error.strerror or error}. Setup runs as root and must be able to "
              f"create folders there and give them to PUID {PUID} (NFS with root_squash and CIFS refuse this)",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
