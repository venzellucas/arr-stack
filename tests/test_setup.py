import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("setup", Path(__file__).resolve().parents[1] / "scripts/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class PasswordHashTests(unittest.TestCase):
    def test_hash_is_qbittorrents_pbkdf2_format(self):
        salt = bytes(range(16))
        value = setup.password_hash("secret", salt)
        match = re.fullmatch(r"@ByteArray\(([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)\)", value)
        self.assertIsNotNone(match)
        self.assertEqual(base64.b64decode(match.group(1)), salt)
        expected = hashlib.pbkdf2_hmac("sha512", b"secret", salt, 100000, 64)
        self.assertEqual(base64.b64decode(match.group(2)), expected)

    def test_new_salt_each_time(self):
        self.assertNotEqual(setup.password_hash("same"), setup.password_hash("same"))

    def test_conf_has_the_sections_qbittorrent_reads(self):
        conf = setup.qbittorrent_conf("admin", "secret", bytes(16))
        self.assertIn("[LegalNotice]\nAccepted=true\n", conf)
        self.assertIn("[BitTorrent]\nSession\\DefaultSavePath=/data/downloads\n", conf)
        self.assertIn("[WebUI]\nUsername=admin\nPassword_PBKDF2=\"@ByteArray(", conf)


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        setup.APPDATA, setup.DATA = root / "appdata", root / "data"
        setup.DATA.mkdir()
        setup.PUID, setup.PGID = os.getuid(), os.getgid()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_seed_makes_folders_conf_and_saves_the_password_first(self):
        setup.seed()
        for folder in ("downloads", "media/movies", "media/tv"):
            self.assertTrue((setup.DATA / folder).is_dir())
        conf = setup.qbittorrent_conf_path()
        self.assertEqual(stat.S_IMODE(conf.stat().st_mode), 0o600)
        creds = json.loads(setup.creds_path().read_text())
        self.assertEqual(creds["qbittorrent"]["username"], "admin")
        self.assertGreaterEqual(len(creds["qbittorrent"]["password"]), 24)
        self.assertEqual(stat.S_IMODE(setup.creds_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(setup.creds_path().parent.stat().st_mode), 0o700)

    def test_second_seed_changes_nothing(self):
        setup.seed()
        conf, creds = setup.qbittorrent_conf_path().read_text(), setup.creds_path().read_text()
        setup.seed()
        self.assertEqual(setup.qbittorrent_conf_path().read_text(), conf)
        self.assertEqual(setup.creds_path().read_text(), creds)

    def test_existing_qbittorrent_is_left_alone_and_no_password_is_invented(self):
        conf = setup.qbittorrent_conf_path()
        conf.parent.mkdir(parents=True)
        conf.write_text("[WebUI]\nUsername=someone\n")
        setup.seed()
        self.assertEqual(conf.read_text(), "[WebUI]\nUsername=someone\n")
        self.assertFalse(setup.creds_path().exists())

    def test_saved_password_is_reused_when_the_conf_is_missing(self):
        setup.save_creds({"qbittorrent": {"username": "admin", "password": "kept"}, "jellyfin": {"x": 1}})
        setup.seed()
        creds = json.loads(setup.creds_path().read_text())
        self.assertEqual(creds["qbittorrent"]["password"], "kept")
        self.assertEqual(creds["jellyfin"], {"x": 1})


class HelperTests(unittest.TestCase):
    def test_listed_needs_the_exact_key(self):
        self.assertEqual(setup.listed({"sources": [3]}, "sources", "sources"), [3])
        for bad in ({"items": [3]}, {"sources": None}, [3], None):
            with self.assertRaises(setup.StepError):
                setup.listed(bad, "sources", "sources")

    def test_remember_keeps_values_already_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup.APPDATA = Path(tmp)
            creds = {}
            first = setup.remember(creds, "holyarrs", username="admin", password="one")
            again = setup.remember(setup.load_creds(), "holyarrs", username="admin", password="two")
            self.assertEqual(first["password"], "one")
            self.assertEqual(again["password"], "one")

    def test_corrupt_credentials_are_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup.APPDATA = Path(tmp)
            setup.private_dir()
            setup.creds_path().write_text("{not json")
            with self.assertRaises(setup.StepError):
                setup.load_creds()
            self.assertEqual(setup.connect(), 1)


LISTS = {"/api/v1/library-folders": "library_folders", "/api/v1/downloaders": "downloaders",
         "/api/v1/media-servers": "media_servers", "/api/v1/sources": "sources"}


class FakeApps:
    """Just enough of qBittorrent, Jellyfin and HolyArrs, keeping state across calls."""

    def __init__(self):
        self.calls = []
        self.unreachable = 0  # how many next calls fail as if the app were still starting
        self.qbit_password = None
        self.jf = {"wizard_done": False, "password": None, "keys": [], "libs": []}
        self.ha = {"needs_setup": True, "password": None, "lists": {key: [] for key in LISTS.values()}}
        self.tests = {}  # list path -> (status, body) its Test answers
        self.list_answer = {}  # list path -> body its GET answers instead of the real list
        self.done_status = 204

    def handle(self, base, method, path, body):
        self.calls.append((base, method, path))
        if self.unreachable:
            self.unreachable -= 1
            raise setup.StepError(f"{method} {path}: connection refused")
        path, _, query = path.partition("?")
        if base == setup.QBITTORRENT and path == "/api/v2/auth/login":
            return (204, None) if body["password"] == self.qbit_password else (200, "Fails.")
        if base == setup.JELLYFIN:
            return self.jellyfin(method, path, query, body)
        if base == setup.HOLYARRS:
            return self.holyarrs(method, path, body)
        raise AssertionError(f"unexpected call {base} {method} {path}")

    def jellyfin(self, method, path, query, body):
        jf = self.jf
        if path == "/System/Info/Public":
            return 200, {"StartupWizardCompleted": jf["wizard_done"]}
        if path in ("/Startup/Configuration", "/Startup/User") and method == "GET":
            return 200, {}
        if path == "/Startup/User":
            jf["password"] = body["Password"]
            return 204, None
        if path == "/Startup/Complete":
            jf["wizard_done"] = True
            return 204, None
        if path == "/Startup/Configuration":
            return 204, None
        if path == "/Users/AuthenticateByName":
            return (200, {"AccessToken": "session"}) if body["Pw"] == jf["password"] else (401, "Error processing request.")
        if path == "/Auth/Keys" and method == "GET":
            return 200, {"Items": jf["keys"]}
        if path == "/Auth/Keys":
            jf["keys"].append({"AppName": "HolyArrs", "AccessToken": "jellyfin-key"})
            return 204, None
        if path == "/Library/VirtualFolders" and method == "GET":
            return 200, jf["libs"]
        if path == "/Library/VirtualFolders":
            jf["libs"].append({"Locations": [p["Path"] for p in body["LibraryOptions"]["PathInfos"]]})
            return 204, None
        raise AssertionError(f"unexpected Jellyfin call {method} {path}")

    def holyarrs(self, method, path, body):
        ha = self.ha
        if path == "/api/v1/auth/state":
            return 200, {"needs_setup": ha["needs_setup"]}
        if path == "/api/v1/auth/setup":
            ha["needs_setup"], ha["password"] = False, body["password"]
            return 200, {"name": body["name"], "csrf": "csrf"}
        if path == "/api/v1/auth/login":
            return (200, {"name": body["name"], "csrf": "csrf"}) if body["password"] == ha["password"] else (401, {"error": "bad"})
        if path == "/api/v1/auth/setup/done":
            return self.done_status, None
        if path in LISTS and method == "GET":
            return 200, self.list_answer.get(path, {LISTS[path]: ha["lists"][LISTS[path]]})
        if path in LISTS:
            items = ha["lists"][LISTS[path]]
            item = dict(body, id=len(items) + 1)
            items.append(item)
            return 201, item
        if path.endswith("/test"):
            return self.tests.get(path.rsplit("/", 2)[0], (200, {"ok": True}))
        raise AssertionError(f"unexpected HolyArrs call {method} {path}")


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        setup.APPDATA = Path(self.tmp.name)
        self.apps = apps = FakeApps()

        class FakeClient(setup.Client):
            def call(self, method, path, body=None, form=False, timeout=setup.TIMEOUT):
                return apps.handle(self.base, method, path, body)

        self.saved = setup.Client, setup.WAIT, setup.time.sleep
        setup.Client, setup.WAIT, setup.time.sleep = FakeClient, 2, lambda seconds: None
        # A fresh install: seed gave qBittorrent its generated password.
        apps.qbit_password = setup.remember({}, "qbittorrent", username="admin", password="qbit")["password"]

    def tearDown(self):
        setup.Client, setup.WAIT, setup.time.sleep = self.saved
        self.tmp.cleanup()

    def creds(self):
        return setup.load_creds()

    def lists(self):
        return {key: len(items) for key, items in self.apps.ha["lists"].items()}

    def test_fresh_install_connects_everything_and_marks_it_done(self):
        self.assertEqual(setup.connect(), 0)
        self.assertEqual(self.lists(), {"library_folders": 2, "downloaders": 1, "media_servers": 1, "sources": 2})
        creds = self.creds()
        self.assertTrue(creds["holyarrs"]["done"] and creds["jellyfin"]["done"])
        self.assertEqual(creds["jellyfin"]["api_key"], "jellyfin-key")
        self.assertEqual(len(self.apps.jf["libs"]), 2)
        downloader = self.apps.ha["lists"]["downloaders"][0]
        self.assertEqual((downloader["username"], downloader["secret"]), ("admin", "qbit"))

    def test_a_connected_install_is_never_touched_again(self):
        setup.connect()
        self.apps.calls.clear()
        self.assertEqual(setup.connect(), 0)
        self.assertEqual(self.apps.calls, [])

    def test_qbittorrent_still_starting_is_waited_for(self):
        self.apps.unreachable = 2
        self.assertEqual(setup.connect(), 0)
        self.assertEqual(self.lists()["downloaders"], 1)

    def test_a_changed_qbittorrent_password_is_tried_once_and_left_alone(self):
        self.apps.qbit_password = "changed by hand"
        self.assertEqual(setup.connect(), 0)
        logins = [c for c in self.apps.calls if c[0] == setup.QBITTORRENT]
        self.assertEqual(len(logins), 1)  # never retried: qBittorrent bans after 5 failures
        self.assertEqual(self.lists()["downloaders"], 0)

    def test_a_holyarrs_owner_made_elsewhere_is_left_alone(self):
        self.apps.ha.update(needs_setup=False, password="theirs")
        self.assertEqual(setup.connect(), 0)
        self.assertEqual(sum(self.lists().values()), 0)
        self.assertNotIn("holyarrs", self.creds())

    def test_a_changed_holyarrs_password_is_left_alone(self):
        setup.remember(self.creds(), "holyarrs", username="admin", password="ours")
        self.apps.ha.update(needs_setup=False, password="changed by hand")
        self.assertEqual(setup.connect(), 0)
        self.assertEqual(sum(self.lists().values()), 0)
        self.assertNotIn("done", self.creds()["holyarrs"])

    def test_a_jellyfin_set_up_elsewhere_is_left_alone(self):
        self.apps.jf.update(wizard_done=True, password="theirs")
        self.assertEqual(setup.connect(), 0)
        self.assertEqual(self.apps.jf["keys"], [])
        self.assertEqual(self.lists()["media_servers"], 0)
        self.assertEqual(self.lists()["downloaders"], 1)

    def test_a_torrent_site_that_is_down_only_warns(self):
        self.apps.tests["/api/v1/sources"] = (200, {"ok": False, "error": "timeout"})
        self.assertEqual(setup.connect(), 0)
        self.assertTrue(self.creds()["holyarrs"]["done"])

    def test_a_failing_local_test_fails_the_run_and_a_rerun_adds_nothing_twice(self):
        self.apps.tests["/api/v1/downloaders"] = (200, {"ok": False, "error": "refused"})
        self.assertEqual(setup.connect(), 1)
        self.assertNotIn("done", self.creds()["holyarrs"])
        self.assertEqual(self.lists()["sources"], 2)  # the rest still ran
        del self.apps.tests["/api/v1/downloaders"]
        self.assertEqual(setup.connect(), 0)
        self.assertEqual(self.lists(), {"library_folders": 2, "downloaders": 1, "media_servers": 1, "sources": 2})
        self.assertTrue(self.creds()["holyarrs"]["done"])

    def test_first_run_not_marked_finished_is_a_failure(self):
        self.apps.done_status = 500
        self.assertEqual(setup.connect(), 1)
        self.assertNotIn("done", self.creds()["holyarrs"])

    def test_an_unexpected_list_shape_is_a_failure(self):
        self.apps.list_answer["/api/v1/downloaders"] = {"items": []}
        self.assertEqual(setup.connect(), 1)
        self.assertEqual(self.lists()["downloaders"], 0)

    def test_one_apps_unexpected_error_does_not_stop_the_others(self):
        jellyfin = self.apps.jellyfin
        self.apps.jellyfin = lambda m, p, q, b: (200, {}) if p == "/Users/AuthenticateByName" else jellyfin(m, p, q, b)
        self.assertEqual(setup.connect(), 1)  # KeyError: no AccessToken
        self.assertEqual(self.lists()["downloaders"], 1)
        self.assertTrue(self.creds()["holyarrs"]["done"])

    def test_a_2xx_answer_that_is_not_json_is_an_error(self):
        client = setup.Client("http://x")
        client.call = lambda *args, **kwargs: (200, "<html>proxy page</html>")
        with self.assertRaises(setup.StepError):
            client.ok("GET", "/api/v1/auth/state")


class EnvFileTests(unittest.TestCase):
    """check.sh reads .env the way Compose does."""

    def read(self, env, name="CONFIG_DIR", mode="credentials"):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            script = Path(tmp) / "scripts" / "check.sh"
            script.write_text((Path(__file__).resolve().parents[1] / "scripts" / "check.sh").read_text())
            (Path(tmp) / ".env").write_text(env)
            result = subprocess.run(["sh", str(script), mode], capture_output=True, text=True)
            return result.returncode, result.stdout.splitlines()[0] if result.stdout else ""

    def config_dir(self, env):
        return self.read(env)[1].removesuffix("/stack-setup/credentials.json")

    def test_values(self):
        cases = {
            "": "/opt/appdata",
            "CONFIG_DIR=/srv/apps\n": "/srv/apps",
            "CONFIG_DIR=/srv/apps # where the apps keep config\n": "/srv/apps",
            "  CONFIG_DIR = /srv/apps  \n": "/srv/apps",
            'CONFIG_DIR="/srv/my apps" # quoted\n': "/srv/my apps",
            "CONFIG_DIR='/srv/a#b'\n": "/srv/a#b",
            "CONFIG_DIR=/srv/a#b\n": "/srv/a#b",
            "CONFIG_DIR=/one\nCONFIG_DIR=/two\n": "/two",
            "#CONFIG_DIR=/commented\n": "/opt/appdata",
            "CONFIG_DIRS=/other\n": "/opt/appdata",
            "CONFIG_DIR=\n": "/opt/appdata",
        }
        for env, expected in cases.items():
            with self.subTest(env=env):
                self.assertEqual(self.config_dir(env), expected)

    def test_updater_switch(self):
        for env, on in {"": False, "COMPOSE_PROFILES=updater\n": True, "COMPOSE_PROFILES=gpu,updater # weekly\n": True,
                        "#COMPOSE_PROFILES=updater\n": False, "COMPOSE_PROFILES=updaters\n": False}.items():
            with self.subTest(env=env):
                self.assertEqual(self.read(env, mode="updater-on")[0] == 0, on)


if __name__ == "__main__":
    unittest.main()
