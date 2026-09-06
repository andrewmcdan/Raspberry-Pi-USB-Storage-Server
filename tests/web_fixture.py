"""Isolated Linux web fixture; also usable as a local browser-test server."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "opt/piusb/web"
sys.path.insert(0, str(WEB))


class WebFixture:
    def __init__(self, enabled=None):
        self.temp = tempfile.TemporaryDirectory(prefix="piusb-web-test-")
        self.root = Path(self.temp.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        (self.staging / "jobs").mkdir()
        self.source = b"G90\nM83\nG1 Z0.2\nG1 X20 E1\nG1 Y20 E1\nG1 X0 E1\nG1 Y0 E1\nG1 Z0.4\nG1 X20 E1\nG1 Y20 E1\nG1 X0 E1\nG1 Y0 E1\n"
        (self.staging / "jobs/part.GCODE").write_bytes(self.source)
        (self.staging / "notes.txt").write_text("Keep me", encoding="utf-8")
        config = self.root / "piusb.ini"
        config.write_text("[piusb]\n" + "".join(f"{key}_dir = {self.root / folder}\n" for key, folder in
                          [("staging", "staging"), ("incoming", "incoming"), ("state", "state"), ("runtime", "runtime")]), encoding="utf-8")
        from werkzeug.security import generate_password_hash
        web_config = self.root / "web.ini"
        web_config.write_text(f"[web]\nusername = admin\npassword_hash = {generate_password_hash('test-only')}\nsecret_key = test-only-key\n" +
                              (f"gcode_viewer = {enabled}\n" if enabled is not None else ""), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("app", WEB / "app.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"PIUSB_CONFIG": str(config), "PIUSB_WEB_CONFIG": str(web_config)}):
            spec.loader.exec_module(self.module)
        self.app = self.module.app
        self.app.testing = True
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["csrf_token"] = "test-csrf"

    def close(self):
        self.temp.cleanup()


if __name__ == "__main__":
    fixture = WebFixture("true")
    print("Test-only login: admin / test-only", flush=True)
    fixture.app.run(host="127.0.0.1", port=8765, use_reloader=False)
