#!/usr/bin/env python3
"""Connect HolyArrs, qBittorrent and Jellyfin on a fresh install. Standard library only.

Runs in the `setup` container: `docker compose run --rm setup seed|connect`.

seed     Before the first start: make the data folders and give qBittorrent a
         generated password.
connect  Once the apps answer: finish Jellyfin's first run, then make the
         HolyArrs owner and connect HolyArrs to qBittorrent, Jellyfin and two
         built-in torrent sites.

Anything already set up is left as it is. Each generated password or key is
saved to credentials.json before it is used, so a failed run can be resumed.
"""

import base64
import hashlib
import http.cookiejar
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APPDATA = Path("/appdata")
DATA = Path("/data")
HOST_CONFIG_DIR = os.environ.get("CONFIG_DIR", "/opt/appdata")
PUID = int(os.environ.get("PUID", "1000"))
PGID = int(os.environ.get("PGID", "1000"))

QBITTORRENT = "http://qbittorrent:8080"
JELLYFIN = "http://jellyfin:8096"
HOLYARRS = "http://holyarrs:6262"

TIMEOUT = 30  # seconds per request
WAIT = 180  # seconds an app may take to become ready

FOLDERS = (("/data/media/movies", "movie", "Movies", "movies"), ("/data/media/tv", "show", "TV", "tvshows"))
SITES = (("knaben", "Knaben"), ("torrentscsv", "TorrentsCSV"))


class StepError(Exception):
    """A request failed; the message names the request and its answer."""


# --- credentials -----------------------------------------------------------

def creds_path():
    return APPDATA / "stack-setup" / "credentials.json"


def load_creds():
    path = creds_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_creds(creds):
    path = creds_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(creds, handle, indent=2)
    os.replace(tmp, path)


def remember(creds, app, **values):
    """Save values for app before they are used, keeping any already saved."""
    entry = creds.setdefault(app, {})
    for key, value in values.items():
        entry.setdefault(key, value)
    save_creds(creds)
    return entry


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
    fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(qbittorrent_conf(login["username"], login["password"]))
    os.chown(conf, PUID, PGID)
    print("qBittorrent: password generated, downloads go to /data/downloads")


# --- HTTP ------------------------------------------------------------------

class Client:
    def __init__(self, base, host=None, headers=None):
        self.base = base
        self.headers = dict(headers or {})
        if host:
            self.headers["Host"] = host
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, form=False):
        headers = dict(self.headers)
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        except (urllib.error.URLError, OSError) as error:
            raise StepError(f"{method} {path}: {error}") from None
        try:
            return status, (json.loads(raw) if raw else None)
        except ValueError:
            return status, raw.decode(errors="replace")

    def ok(self, method, path, body=None, form=False):
        status, out = self.call(method, path, body, form)
        if not 200 <= status < 300:
            raise StepError(f"{method} {path} answered {status}: {str(out)[:200]}")
        return out

    def wait(self, path):
        """GET path until it answers 2xx, or fail after WAIT seconds."""
        deadline = time.monotonic() + WAIT
        while True:
            try:
                status, out = self.call("GET", path)
            except StepError as error:
                status, out = 0, str(error)
            if 200 <= status < 300:
                return out
            if time.monotonic() > deadline:
                raise StepError(f"GET {path} was not ready after {WAIT}s (last answer {status}: {str(out)[:120]})")
            time.sleep(3)


def items(out, *keys):
    """The list in an API answer, whether bare or under one of keys."""
    if isinstance(out, list):
        return out
    for key in keys:
        if isinstance(out, dict) and isinstance(out.get(key), list):
            return out[key]
    return []


# --- connect ---------------------------------------------------------------

def check_qbittorrent(creds):
    login = creds.get("qbittorrent")
    if not login:
        print("qBittorrent: set up before this script; HolyArrs's Downloader is left for you")
        return None
    qb = Client(QBITTORRENT)
    deadline = time.monotonic() + WAIT
    while True:
        status, out = qb.call("POST", "/api/v2/auth/login", {"username": login["username"], "password": login["password"]}, form=True)
        # Older versions answer 200 "Ok.", newer ones 204 with no body.
        if status == 204 or (status == 200 and str(out).strip() == "Ok."):
            print("qBittorrent: signs in with the generated password")
            return login
        if time.monotonic() > deadline:
            raise StepError(f"sign-in with the saved password failed ({status}: {str(out)[:120]})")
        time.sleep(3)


def jellyfin_client(token=None):
    auth = 'MediaBrowser Client="media-stack", Device="setup", DeviceId="media-stack-setup", Version="1.0"'
    if token:
        auth += f', Token="{token}"'
    return Client(JELLYFIN, headers={"Authorization": auth})


def setup_jellyfin(creds):
    jf = jellyfin_client()
    info = jf.wait("/System/Info/Public")
    saved = creds.get("jellyfin", {})
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
    auth = jf.ok("POST", "/Users/AuthenticateByName", {"Username": login["username"], "Pw": login["password"]})
    jf = jellyfin_client(auth["AccessToken"])
    if not login.get("api_key"):
        if not any(k.get("AppName") == "HolyArrs" for k in items(jf.ok("GET", "/Auth/Keys"), "Items")):
            jf.ok("POST", "/Auth/Keys?" + urllib.parse.urlencode({"app": "HolyArrs"}))
        key = next(k["AccessToken"] for k in items(jf.ok("GET", "/Auth/Keys"), "Items") if k.get("AppName") == "HolyArrs")
        login["api_key"] = key
        save_creds(creds)
    libraries = items(jf.ok("GET", "/Library/VirtualFolders"))
    for path, _, label, collection in FOLDERS:
        if not any(path in lib.get("Locations", []) for lib in libraries):
            query = urllib.parse.urlencode({"name": label, "collectionType": collection, "refreshLibrary": "false"})
            jf.ok("POST", "/Library/VirtualFolders?" + query, {"LibraryOptions": {"PathInfos": [{"Path": path}]}})
            print(f"Jellyfin: library {label} added ({path})")
    return login["api_key"]


def test(ha, path, ident, name):
    status, out = ha.call("POST", f"{path}/{ident}/test")
    passed = 200 <= status < 300 and (not isinstance(out, dict) or out.get("ok", True))
    if not passed:
        raise StepError(f"{name} was added but its Test failed ({status}: {str(out)[:160]})")
    print(f"HolyArrs: {name} passes Test")


def setup_holyarrs(creds, qbit, jellyfin_key):
    # Host: localhost is always accepted and saves nothing, with or without a proxy.
    ha = Client(HOLYARRS, host="localhost")
    state = ha.wait("/api/v1/auth/state")
    saved = creds.get("holyarrs", {})
    if state.get("needs_setup"):
        login = remember(creds, "holyarrs", username="admin", password=new_password())
        me = ha.ok("POST", "/api/v1/auth/setup", {"name": login["username"], "password": login["password"]})
        print("HolyArrs: owner created")
    elif "password" in saved:
        me = ha.ok("POST", "/api/v1/auth/login", {"name": saved["username"], "password": saved["password"]})
    else:
        print("HolyArrs: has an owner this script did not make; left as it is")
        return
    ha.headers["X-CSRF-Token"] = me["csrf"]

    folders = items(ha.ok("GET", "/api/v1/library-folders"), "folders", "library_folders")
    for path, kind, label, _ in FOLDERS:
        if not any(f.get("path") == path for f in folders):
            ha.ok("POST", "/api/v1/library-folders", {"path": path, "kind": kind, "label": label})
            print(f"HolyArrs: library folder {path} added")

    if qbit:
        found = items(ha.ok("GET", "/api/v1/downloaders"), "downloaders")
        mine = next((d for d in found if d.get("kind") == "qbittorrent" and d.get("url") == QBITTORRENT), None)
        if not mine:
            mine = ha.ok("POST", "/api/v1/downloaders", {
                "name": "qBittorrent", "kind": "qbittorrent", "url": QBITTORRENT, "username": qbit["username"],
                "secret": qbit["password"], "category": "holyarrs", "allow_local_network": True})
            mine = mine.get("downloader", mine)
        test(ha, "/api/v1/downloaders", mine["id"], "qBittorrent")
    else:
        print("HolyArrs: Downloader skipped (no saved qBittorrent password)")

    if jellyfin_key:
        found = items(ha.ok("GET", "/api/v1/media-servers"), "servers", "media_servers")
        mine = next((m for m in found if m.get("kind") == "jellyfin" and m.get("url") == JELLYFIN), None)
        if not mine:
            mine = ha.ok("POST", "/api/v1/media-servers", {
                "name": "Jellyfin", "kind": "jellyfin", "url": JELLYFIN, "token": jellyfin_key, "enabled": True,
                "on_import": True, "on_rename": True, "on_delete": True, "allow_local_network": True})
            mine = mine.get("server", mine.get("media_server", mine))
        test(ha, "/api/v1/media-servers", mine["id"], "Jellyfin")
    else:
        print("HolyArrs: media server skipped (no saved Jellyfin key)")

    found = items(ha.ok("GET", "/api/v1/sources"), "sources")
    for site, name in SITES:
        mine = next((s for s in found if s.get("site") == site), None)
        if not mine:
            mine = ha.ok("POST", "/api/v1/sources", {"name": name, "format": "site", "site": site})
            mine = mine.get("source", mine)
        test(ha, "/api/v1/sources", mine["id"], name)

    status, out = ha.call("POST", "/api/v1/auth/setup/done")
    if not 200 <= status < 300:
        print(f"HolyArrs: could not mark first run as finished ({status}); finish it in the browser")


def connect():
    creds = load_creds()
    failed = []

    def step(name, action):
        try:
            return action()
        except (StepError, KeyError, TypeError) as error:
            print(f"FAIL {name}: {error}", file=sys.stderr)
            failed.append(name)
            return None

    qbit = step("qBittorrent", lambda: check_qbittorrent(creds))
    jellyfin_key = step("Jellyfin", lambda: setup_jellyfin(creds))
    step("HolyArrs", lambda: setup_holyarrs(creds, qbit, jellyfin_key))
    if creds_path().exists():
        print(f"Passwords and keys: {HOST_CONFIG_DIR}/stack-setup/credentials.json (root only)")
    if failed:
        print("Fix the failure above and run `make up` again; finished steps are kept.", file=sys.stderr)
    return 1 if failed else 0


def main(argv):
    if argv[1:] == ["seed"]:
        seed()
        return 0
    if argv[1:] == ["connect"]:
        return connect()
    print("usage: setup.py seed|connect", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
