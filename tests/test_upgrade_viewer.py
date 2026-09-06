import configparser
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("upgrade", ROOT / "scripts/upgrade_gcode_viewer.py")
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)

OLD_CONFIG = "# keep comments\n[web] # existing login\nusername = owner\npassword_hash = scrypt:32768:8:1$abc$123\nsecret_key = session%key\ncustom_option = keep\n\n[other]\nvalue = 42\n"


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="piusb-upgrade-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.web = self.root / "opt/piusb/web"
        self.web.mkdir(parents=True)
        self.config = self.root / "etc/piusb/web.ini"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(OLD_CONFIG)
        self.config.chmod(0o640)
        (self.web / "app.py").write_text("# existing app\n")
        self.main = self.config.with_name("piusb.ini")
        self.main.write_text("[piusb]\nimage_size_mib = 8192\n")
        self.protected = [self.main, self.root / "staging/print.gcode", self.root / "images/a.img",
                          self.root / "images/b.img", self.root / "state/active", self.root / "piusb-web.service"]
        for path in self.protected[1:]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unchanged: " + path.name)
        self.original_protected = {p: p.read_bytes() for p in self.protected}
        for name, value in {"WEB": self.web, "CONFIG": self.config, "MAIN_CONFIG": self.main,
                            "BACKUPS": self.root / "backups"}.items():
            patcher = patch.object(upgrade, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.group = patch.object(upgrade.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid()))
        self.group.start(); self.addCleanup(self.group.stop)
        self.chown = patch.object(upgrade.os, "chown")
        self.chown.start(); self.addCleanup(self.chown.stop)
        self.calls = []
        self.active = True
        self.fail_validation = False
        self.failed_start = False
        self.fail_start = False
        self.runner = patch.object(upgrade.subprocess, "run", side_effect=self.run_command)
        self.runner.start(); self.addCleanup(self.runner.stop)

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        if command[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(command, 0, "loaded\n")
        if command[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(command, 0 if self.active else 3)
        if command[0] == "runuser" and self.fail_validation:
            raise subprocess.CalledProcessError(1, command)
        if command[:2] == ["systemctl", "start"] and self.fail_start and not self.failed_start:
            self.failed_start = True
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    def assert_preserved(self):
        for path, data in self.original_protected.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        for command in self.calls:
            if command[0] == "systemctl":
                self.assertEqual(command[-1], "piusb-web.service")

    def test_upgrade_preserves_credentials_data_and_customizations(self):
        upgrade.upgrade()
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read(self.config)
        original = configparser.ConfigParser(interpolation=None)
        original.read_string(OLD_CONFIG)
        for key, value in original["web"].items():
            self.assertEqual(parsed["web"][key], value)
        self.assertTrue(parsed.getboolean("web", "gcode_viewer"))
        self.assertIn("# keep comments", self.config.read_text())
        self.assertTrue((self.web / "gcode_viewer/static/viewer.js").is_file())
        self.assertEqual((self.web / "gcode_viewer/VERSION").read_text(), (ROOT / "VERSION").read_text())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertTrue(any((self.root / "backups").glob("gcode-viewer-*/files.txt")))
        self.assert_preserved()

    def test_repeated_upgrade_and_disabled_setting(self):
        self.config.write_text(OLD_CONFIG.replace("custom_option = keep", "gcode_viewer: false\ncustom_option = keep"))
        upgrade.upgrade()
        first = self.config.read_bytes()
        upgrade.upgrade()
        self.assertEqual(self.config.read_bytes(), first)
        self.assertEqual(self.config.read_text().count("gcode_viewer"), 1)
        self.assert_preserved()

    def test_validation_and_service_start_failure_roll_back(self):
        for failure in ("fail_validation", "fail_start"):
            with self.subTest(failure=failure):
                setattr(self, failure, True)
                with self.assertRaises(subprocess.CalledProcessError):
                    upgrade.upgrade()
                self.assertEqual(self.config.read_text(), OLD_CONFIG)
                self.assertEqual((self.web / "app.py").read_text(), "# existing app\n")
                self.assertFalse((self.web / "gcode_viewer/__init__.py").exists())
                self.assertFalse((self.web / "gcode_viewer/VERSION").exists())
                self.assert_preserved()
                setattr(self, failure, False)

    def test_stopped_service_is_not_started(self):
        self.active = False
        upgrade.upgrade()
        self.assertFalse(any(c[:2] in (["systemctl", "stop"], ["systemctl", "start"]) for c in self.calls))
        self.assert_preserved()

    def test_missing_installation_and_invalid_credentials_fail_before_stop(self):
        self.config.write_text("[web]\nusername = owner\n")
        with self.assertRaises(RuntimeError):
            upgrade.upgrade()
        self.assertEqual(self.calls, [])
        self.config.unlink()
        with self.assertRaises(RuntimeError):
            upgrade.upgrade()
        self.assertEqual(self.calls, [])

    def test_fresh_assets_dont_replace_web_app_or_config(self):
        upgrade.preflight(upgrade.ADDON_FILES)
        upgrade.install_assets(upgrade.ADDON_FILES)
        self.assertEqual((self.web / "app.py").read_text(), "# existing app\n")
        self.assertEqual(self.config.read_text(), OLD_CONFIG)
        self.assertTrue((self.web / "gcode_viewer/static/parser.js").is_file())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
