import io
import os
import unittest
from urllib.parse import quote
from unittest.mock import patch

from tests.web_fixture import WebFixture


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = WebFixture("true")
        self.addCleanup(self.fixture.close)
        self.client = self.fixture.client

    def test_authentication_covers_page_source_and_assets(self):
        for path in ("/gcode/jobs/part.GCODE", "/gcode-assets/worker.js", "/gcode-assets/parser.js"):
            self.assertEqual(self.client.get(path).status_code, 302)
        self.assertEqual(self.client.get("/api/gcode/file?path=jobs/part.GCODE").status_code, 401)

    def test_preview_links_page_and_source(self):
        self.fixture.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('/gcode/jobs/part.GCODE', page)
        self.assertNotIn('/gcode/notes.txt', page)
        self.assertEqual(self.client.get("/gcode/jobs/part.GCODE").status_code, 200)
        response = self.client.get("/api/gcode/file?path=jobs/part.GCODE")
        self.addCleanup(response.close)
        self.assertEqual(response.data, self.fixture.source)
        self.assertEqual(response.content_length, len(self.fixture.source))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response.content_type.startswith("text/plain"))
        for filename in ("worker.js", "parser.js", "viewer.js", "viewer.css"):
            response = self.client.get(f"/gcode-assets/{filename}")
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 200)

    def test_default_and_explicit_disabled_need_no_addon(self):
        for enabled in (None, "false"):
            fixture = WebFixture(enabled)
            self.addCleanup(fixture.close)
            fixture.login()
            self.assertNotIn("gcode_viewer", fixture.app.blueprints)
            self.assertNotIn("Preview</a>", fixture.client.get("/").get_data(as_text=True))
            for path in ("/gcode/jobs/part.GCODE", "/api/gcode/file?path=jobs/part.GCODE", "/gcode-assets/viewer.js"):
                self.assertEqual(fixture.client.get(path).status_code, 404)

    def test_paths_symlinks_missing_and_file_types(self):
        self.fixture.login()
        (self.fixture.root / "secret.gcode").write_text("private")
        (self.fixture.staging / "link.gcode").symlink_to(self.fixture.root / "secret.gcode")
        (self.fixture.staging / "linked-dir").symlink_to(self.fixture.root, target_is_directory=True)
        (self.fixture.staging / "directory.gcode").mkdir()
        os.mkfifo(self.fixture.staging / "pipe.gcode")
        for path in ("../secret.gcode", "/etc/passwd.gcode", "link.gcode", "linked-dir/secret.gcode", "missing.gcode", "directory.gcode", "pipe.gcode", "jobs/../../secret.gcode"):
            response = self.client.get("/api/gcode/file?path=" + quote(path, safe=""))
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.client.get("/api/gcode/file?path=notes.txt").status_code, 415)

    def test_size_limit_does_not_prevent_download(self):
        self.fixture.login()
        with patch("gcode_viewer.MAX_PREVIEW_BYTES", 8):
            self.assertEqual(self.client.get("/gcode/jobs/part.GCODE").status_code, 422)
            self.assertEqual(self.client.get("/api/gcode/file?path=jobs/part.GCODE").status_code, 422)
            response = self.client.get("/files/jobs/part.GCODE")
            self.addCleanup(response.close)
            self.assertEqual(response.data, self.fixture.source)

    def test_open_preview_survives_atomic_replacement(self):
        self.fixture.login()
        response = self.client.get("/api/gcode/file?path=jobs/part.GCODE", buffered=False)
        self.addCleanup(response.close)
        replacement = self.fixture.staging / "replacement"
        replacement.write_bytes(b"new contents")
        replacement.replace(self.fixture.staging / "jobs/part.GCODE")
        self.assertEqual(response.data, self.fixture.source)

    def test_upload_publish_and_download_still_work(self):
        self.fixture.login()
        original = self.fixture.module.staging_summary()["manifest"]
        response = self.client.get("/api/gcode/file?path=jobs/part.GCODE")
        response.close()
        self.assertEqual(self.fixture.module.staging_summary()["manifest"], original)
        self.assertFalse(self.fixture.module.PUBLISH_REQUEST.exists())
        result = self.client.post("/api/uploads/file", data={"csrf_token": "test-csrf", "folder": "jobs", "file": (io.BytesIO(b"G1 X1 E1\n"), "new.gcode")})
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.client.post("/publish", data={"csrf_token": "test-csrf"}).status_code, 302)
        self.assertTrue(self.fixture.module.PUBLISH_REQUEST.exists())


if __name__ == "__main__":
    unittest.main()
