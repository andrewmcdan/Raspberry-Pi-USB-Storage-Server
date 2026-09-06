#!/usr/bin/env python3
"""Install the optional web add-on without changing the USB publisher or data.

The full installer uses --fresh-assets; the upgrade wrapper uses the backed-up,
transactional update. Only the web service is stopped, and only if it is active.
"""

import configparser
import grp
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
WEB = Path("/opt/piusb/web")
CONFIG = Path("/etc/piusb/web.ini")
MAIN_CONFIG = Path("/etc/piusb/piusb.ini")
BACKUPS = Path("/var/backups/piusb")
ADDON_FILES = (
    "gcode_viewer/__init__.py",
    "gcode_viewer/templates/gcode_viewer.html",
    "gcode_viewer/static/parser.js",
    "gcode_viewer/static/worker.js",
    "gcode_viewer/static/viewer.js",
    "gcode_viewer/static/viewer.css",
)
UPGRADE_FILES = ("app.py", "templates/index.html", "templates/login.html", *ADDON_FILES)


def preflight(files):
    for relative in files:
        source = SOURCE / "opt/piusb/web" / relative
        if not source.is_file():
            raise RuntimeError(f"Incomplete release bundle: missing {source}")
        if source.suffix == ".py":
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
    if not (SOURCE / "VERSION").is_file():
        raise RuntimeError("Incomplete release bundle: missing VERSION")


def atomic_copy(source, destination, mode=0o644):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary = tempfile.mkstemp(prefix=".gcode-upgrade-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_assets(files):
    for relative in files:
        atomic_copy(SOURCE / "opt/piusb/web" / relative, WEB / relative)
    atomic_copy(SOURCE / "VERSION", WEB / "gcode_viewer/VERSION")


def enabled_config(text):
    """Change only the feature key, preserving credentials, comments and extras."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    if not parser.has_section("web") or not all(parser.get("web", key, fallback="") for key in ("username", "password_hash", "secret_key")):
        raise RuntimeError("Existing web.ini is missing web credentials; run the full installer first.")
    lines = text.splitlines(keepends=True)
    section_start = next(i for i, line in enumerate(lines) if re.match(r"\s*\[web\]", line))
    section_end = next((i for i in range(section_start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(section_start + 1, section_end):
        line = lines[i].lstrip()
        if line.startswith(("#", ";")):
            continue
        key = line.replace(":", "=", 1).split("=", 1)[0].strip().lower()
        if key == "gcode_viewer":
            lines[i] = "gcode_viewer = true\n"
            break
    else:
        if section_end and not lines[section_end - 1].endswith("\n"):
            lines[section_end - 1] += "\n"
        lines.insert(section_end, "gcode_viewer = true\n")
    updated = "".join(lines)
    check = configparser.ConfigParser(interpolation=None)
    check.read_string(updated)
    if not check.getboolean("web", "gcode_viewer"):
        raise RuntimeError("Could not enable gcode_viewer in web.ini")
    return updated


def systemctl(*args, check=True):
    return subprocess.run(["systemctl", *args, "piusb-web.service"], check=check)


def upgrade():
    preflight(UPGRADE_FILES)
    if not (WEB / "app.py").is_file() or not CONFIG.is_file() or not MAIN_CONFIG.is_file():
        raise RuntimeError("No existing Pi USB Publisher installation found. Use install.sh for a fresh install.")
    group_id = grp.getgrnam("piusb").gr_gid
    new_config = enabled_config(CONFIG.read_text(encoding="utf-8"))
    # Import availability and template syntax are checked before stopping service.
    from flask import Flask
    from jinja2 import Environment
    for relative in UPGRADE_FILES:
        if relative.endswith(".html"):
            Environment().parse((SOURCE / "opt/piusb/web" / relative).read_text(encoding="utf-8"))
    load = subprocess.run(["systemctl", "show", "--property=LoadState", "--value", "piusb-web.service"],
                          check=True, capture_output=True, text=True)
    if load.stdout.strip() != "loaded":
        raise RuntimeError("piusb-web.service is not installed or could not be loaded.")
    was_active = systemctl("is-active", "--quiet", check=False).returncode == 0
    if not was_active:
        print("Web service is stopped; it will remain stopped after this upgrade.")
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix="gcode-viewer-", dir=BACKUPS))
    targets = [*(WEB / relative for relative in UPGRADE_FILES), WEB / "gcode_viewer/VERSION", CONFIG]
    records = []
    for index, target in enumerate(targets):
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
            raise RuntimeError(f"Cannot upgrade a symlinked installation path: {target}")
        saved = backup / str(index)
        if target.exists():
            if not target.is_file():
                raise RuntimeError(f"Expected a regular file: {target}")
            shutil.copy2(target, saved)
            info = target.stat()
            records.append((target, saved, info.st_uid, info.st_gid))
        else:
            records.append((target, None, 0, 0))
    (backup / "files.txt").write_text("\n".join(f"{i}: {target}" for i, target in enumerate(targets)) + "\n", encoding="utf-8")
    print(f"Backup saved to {backup}", flush=True)
    try:
        if was_active:
            systemctl("stop")
        install_assets(UPGRADE_FILES)
        pending_config = backup / "enabled-web.ini"
        pending_config.write_text(new_config, encoding="utf-8")
        atomic_copy(pending_config, CONFIG, 0o640)
        os.chown(CONFIG, 0, group_id)
        # Import with the service account before start, exercising actual config,
        # permissions and blueprint registration. app.py cleans only incoming
        # transactions at startup, just as the real web service does.
        subprocess.run(["runuser", "-u", "piusb", "--", "/usr/bin/python3", "-c",
                        "import app; assert 'gcode_viewer' in app.app.blueprints"], cwd=WEB, check=True)
        if was_active:
            systemctl("start")
            systemctl("is-active", "--quiet")
    except BaseException:
        if was_active:
            systemctl("stop", check=False)
        for target, saved, uid, gid in reversed(records):
            if saved is None:
                target.unlink(missing_ok=True)
            else:
                atomic_copy(saved, target, saved.stat().st_mode & 0o777)
                shutil.copystat(saved, target)
                os.chown(target, uid, gid)
        if was_active:
            systemctl("start", check=False)
        print(f"Upgrade failed; previous files restored. Backup: {backup}", file=sys.stderr)
        raise
    print("G-code viewer installed and enabled. Refresh the staged-file list to see Preview links.")
    print("USB images, staging, publisher configuration, and web credentials have been preserved.")
    if not was_active:
        print("Start the web UI when ready: sudo systemctl start piusb-web.service")


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run this installer as root.")
    if sys.argv[1:] == ["--fresh-assets"]:
        preflight(ADDON_FILES)
        install_assets(ADDON_FILES)
    elif not sys.argv[1:]:
        upgrade()
    else:
        raise RuntimeError("Unexpected arguments.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"G-code viewer installation failed: {error}", file=sys.stderr)
        sys.exit(1)
