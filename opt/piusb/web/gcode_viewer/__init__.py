"""Optional, authenticated, read-only G-code preview routes."""

import os
import stat
from pathlib import Path

from flask import Blueprint, abort, render_template, request, send_file

EXTENSIONS = frozenset({".gcode", ".gco", ".g", ".nc"})
MAX_PREVIEW_BYTES = 100 * 1024**2


def create_blueprint(resolve_staged_path):
    viewer = Blueprint(
        "gcode_viewer", __name__, template_folder="templates",
        static_folder="static", static_url_path="/gcode-assets",
    )

    def open_gcode(relative):
        try:
            path = resolve_staged_path(relative)
            if path.suffix.lower() not in EXTENSIONS:
                abort(415, "Preview supports plain-text .gcode, .gco, .g, and .nc files.")
            # O_NONBLOCK avoids hanging a web worker on a special file. Keep
            # the opened descriptor so an atomic re-upload cannot change the
            # file between size validation and streaming it to the browser.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            handle = os.fdopen(descriptor, "rb")
        except (ValueError, OSError):
            abort(404, "The staged file no longer exists or cannot be previewed.")
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            handle.close()
            abort(404)
        if info.st_size > MAX_PREVIEW_BYTES:
            handle.close()
            abort(422, "Preview is limited to 100 MiB. This file can still be downloaded and published.")
        return handle, info.st_size

    @viewer.get("/gcode/<path:relative>")
    def preview(relative):
        handle, size = open_gcode(relative)
        handle.close()
        return render_template("gcode_viewer.html", relative=relative, size=size,
                               max_bytes=MAX_PREVIEW_BYTES,
                               folder=Path(relative).parent.as_posix())

    @viewer.get("/api/gcode/file")
    def source():
        handle, size = open_gcode(request.args.get("path", ""))
        try:
            response = send_file(handle, mimetype="text/plain", as_attachment=True,
                                 download_name="preview.gcode", conditional=False,
                                 etag=False, max_age=0)
        except Exception:
            handle.close()
            raise
        response.content_length = size
        response.call_on_close(handle.close)
        return response

    @viewer.after_request
    def private_preview(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return viewer
