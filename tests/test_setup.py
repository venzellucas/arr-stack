import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
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
    def test_items_reads_bare_and_wrapped_lists(self):
        self.assertEqual(setup.items([1, 2]), [1, 2])
        self.assertEqual(setup.items({"sources": [3]}, "sources"), [3])
        self.assertEqual(setup.items({"servers": [4]}, "media_servers", "servers"), [4])
        self.assertEqual(setup.items({"other": 5}, "sources"), [])
        self.assertEqual(setup.items(None, "sources"), [])

    def test_remember_keeps_values_already_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup.APPDATA = Path(tmp)
            creds = {}
            first = setup.remember(creds, "holyarrs", username="admin", password="one")
            again = setup.remember(setup.load_creds(), "holyarrs", username="admin", password="two")
            self.assertEqual(first["password"], "one")
            self.assertEqual(again["password"], "one")


if __name__ == "__main__":
    unittest.main()
