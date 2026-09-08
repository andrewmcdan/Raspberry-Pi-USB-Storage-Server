#!/usr/bin/env python3
"""Minimal authenticated web interface for Pi USB Publisher."""

from __future__ import annotations

import configparser
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat
import time
import uuid
import unicodedata
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Iterator

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

PIUSB_CONFIG = Path(os.environ.get("PIUSB_CONFIG", "/etc/piusb/piusb.ini"))
WEB_CONFIG = Path(os.environ.get("PIUSB_WEB_CONFIG", "/etc/piusb/web.ini"))
FAT32_MAX_FILE_SIZE = 4 * 1024**3 - 1
RESERVED_DOS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise RuntimeError(f"Configuration file not found: {path}")
    return parser


piusb_ini = read_ini(PIUSB_CONFIG)["piusb"]
web_ini = read_ini(WEB_CONFIG)["web"]

STAGING_DIR = Path(piusb_ini.get("staging_dir", "/srv/piusb/staging")).resolve()
INCOMING_DIR = Path(piusb_ini.get("incoming_dir", "/srv/piusb/incoming")).resolve()
STATE_DIR = Path(piusb_ini.get("state_dir", "/var/lib/piusb")).resolve()
RUNTIME_DIR = Path(piusb_ini.get("runtime_dir", "/run/piusb")).resolve()
STATUS_FILE = STATE_DIR / "status.json"
ACTIVE_FILE = STATE_DIR / "active"
STAGING_LOCK = RUNTIME_DIR / "staging.lock"
PUBLISH_REQUEST = RUNTIME_DIR / "publish.request"
IMAGE_SIZE_MIB = piusb_ini.getint("image_size_mib", 4096)
STAGING_FILL_PERCENT = piusb_ini.getint("staging_fill_percent", 90)
STAGING_LIMIT_BYTES = IMAGE_SIZE_MIB * 1024**2 * STAGING_FILL_PERCENT // 100
WEB_USERNAME = web_ini.get("username", "admin")
PASSWORD_HASH = web_ini.get("password_hash", "")
SECRET_KEY = web_ini.get("secret_key", "")

if not PASSWORD_HASH or not SECRET_KEY:
    raise RuntimeError("web.ini must contain password_hash and secret_key")

STAGING_DIR.mkdir(parents=True, exist_ok=True)
INCOMING_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

# Incomplete upload transactions are never part of staging. Remove leftovers
# from an interrupted request whenever the single-worker web service starts.
for stale_item in INCOMING_DIR.iterdir():
    if stale_item.is_symlink() or not stale_item.is_dir():
        stale_item.unlink(missing_ok=True)
    else:
        shutil.rmtree(stale_item, ignore_errors=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    # Allow multipart framing overhead in addition to the largest accepted file.
    MAX_CONTENT_LENGTH=min(STAGING_LIMIT_BYTES, FAT32_MAX_FILE_SIZE) + 16 * 1024**2,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
)

# Add-on code and assets are only installed when explicitly selected. Old
# web.ini files remain valid and leave the viewer disabled.
GCODE_EXTENSIONS: frozenset[str] = frozenset()
if web_ini.getboolean("gcode_viewer", fallback=False):
    from gcode_viewer import EXTENSIONS, create_blueprint

    GCODE_EXTENSIONS = EXTENSIONS
    app.register_blueprint(create_blueprint(lambda relative: safe_staged_path(relative)))


@contextmanager
def staging_lock(*, blocking: bool) -> Iterator[None]:
    STAGING_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with STAGING_LOCK.open("a+") as handle:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as error:
            raise RuntimeError("A publish or another staging operation is currently running") from error
        try:
            try:
                control = json.loads((STATE_DIR / 'fleet' / 'control.json').read_text())
            except FileNotFoundError:
                control = {}
            if control.get('mode') == 'managed':
                raise RuntimeError('Central manager owns staging; request local takeover first')
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_status() -> dict:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def read_active() -> str:
    try:
        active = ACTIVE_FILE.read_text(encoding="ascii").strip().upper()
    except OSError:
        active = "A"
    return active if active in {"A", "B"} else "A"


def staged_files() -> list[dict]:
    records: list[dict] = []
    for path in sorted(STAGING_DIR.rglob("*"), key=lambda item: item.as_posix().lower()):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        relative = path.relative_to(STAGING_DIR).as_posix()
        records.append(
            {
                "path": relative,
                "size": info.st_size,
                "size_text": human_size(info.st_size),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.st_mtime)),
                "can_preview": path.suffix.lower() in GCODE_EXTENSIONS,
            }
        )
    return records


def staged_folders() -> list[dict]:
    """Return the USB-root folder and every normal staged directory.

    File counts and byte totals include all descendants so a recursive folder
    deletion can be described accurately in the web interface.
    """
    records: dict[str, dict] = {
        "": {
            "path": "",
            "display": "/ (USB drive root)",
            "depth": 0,
            "file_count": 0,
            "total_bytes": 0,
            "total_text": "0 B",
        }
    }

    for path in sorted(STAGING_DIR.rglob("*"), key=lambda item: item.as_posix().lower()):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            continue
        relative = path.relative_to(STAGING_DIR).as_posix()
        records[relative] = {
            "path": relative,
            "display": f"/{relative}",
            "depth": len(PurePosixPath(relative).parts),
            "file_count": 0,
            "total_bytes": 0,
            "total_text": "0 B",
        }

    for file_record in staged_files():
        parent = PurePosixPath(file_record["path"]).parent
        while True:
            relative_parent = "" if str(parent) == "." else parent.as_posix()
            record = records.get(relative_parent)
            if record is not None:
                record["file_count"] += 1
                record["total_bytes"] += file_record["size"]
            if not relative_parent:
                break
            parent = parent.parent

    for record in records.values():
        record["total_text"] = human_size(record["total_bytes"])

    return sorted(
        records.values(),
        key=lambda record: (record["path"] != "", record["path"].casefold()),
    )


def staging_summary() -> dict:
    total = 0
    count = 0
    manifest_records: list[str] = []
    for path in STAGING_DIR.rglob("*"):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        relative = path.relative_to(STAGING_DIR).as_posix()
        if stat.S_ISDIR(info.st_mode):
            manifest_records.append(f"D\0{relative}")
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            count += 1
            manifest_records.append(f"F\0{relative}\0{info.st_size}\0{info.st_mtime_ns}")
        else:
            manifest_records.append(f"X\0{relative}\0{info.st_mode}")
    manifest = hashlib.sha256("\n".join(sorted(manifest_records)).encode("utf-8")).hexdigest()
    return {
        "total_bytes": total,
        "total_text": human_size(total),
        "file_count": count,
        "limit_bytes": STAGING_LIMIT_BYTES,
        "limit_text": human_size(STAGING_LIMIT_BYTES),
        "manifest": manifest,
    }


def status_payload() -> dict:
    status = read_status()
    summary = staging_summary()
    published_manifest = (status.get("active_build") or status.get("build") or {}).get("manifest")
    status.setdefault("state", "unknown")
    status.setdefault("message", "No status has been recorded yet")
    status.setdefault("active", read_active())
    status["request_pending"] = PUBLISH_REQUEST.exists()
    status["staging"] = summary
    status["pending_changes"] = published_manifest != summary["manifest"]
    return status


def human_size(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(number)} {unit}"
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{value} B"


def authenticated() -> bool:
    return bool(session.get("authenticated"))


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_is_valid() -> bool:
    supplied = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    return bool(expected and secrets.compare_digest(supplied, expected))


def verify_csrf() -> None:
    if not csrf_is_valid():
        abort(400, "Invalid CSRF token")



def validate_fat_component(name: str) -> None:
    if not name or name in {".", ".."}:
        raise ValueError(f"Invalid FAT32 name: {name!r}")
    if name.endswith(" ") or name.endswith("."):
        raise ValueError(f"FAT32 names cannot end in a space or period: {name}")
    stem = name.split(".", 1)[0].rstrip(" .").upper()
    if stem in RESERVED_DOS_NAMES:
        raise ValueError(f"Windows reserves this filename: {name}")


def normalized_fat_path(path: Path) -> str:
    relative = path.relative_to(STAGING_DIR).as_posix()
    return unicodedata.normalize("NFC", relative).casefold()


def existing_casefolded_paths() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in STAGING_DIR.rglob("*"):
        relative = path.relative_to(STAGING_DIR).as_posix()
        result[unicodedata.normalize("NFC", relative).casefold()] = relative
    return result

def safe_staged_path(relative: str) -> Path:
    raw = relative.strip().replace("\\", "/")
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or ".." in pure.parts:
        raise ValueError("Invalid staged path")

    candidate = STAGING_DIR
    for part in pure.parts:
        if part in {"", "."}:
            continue
        candidate = candidate / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Symbolic links are not supported in staging")

    if not candidate.resolve(strict=False).is_relative_to(STAGING_DIR):
        raise ValueError("Invalid staged path")
    return candidate


def safe_existing_folder(relative: str) -> Path:
    raw = relative.strip().replace("\\", "/")
    if not raw:
        return STAGING_DIR
    candidate = safe_staged_path(raw)
    try:
        info = candidate.lstat()
    except FileNotFoundError as error:
        raise ValueError("The selected destination folder no longer exists") from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("The selected destination is not a normal folder")
    return candidate


def directory_contents(path: Path) -> tuple[int, int, int]:
    file_count = 0
    folder_count = 0
    total_bytes = 0
    for child in path.rglob("*"):
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Cannot delete a folder tree containing a symbolic link: {child.name}")
        if stat.S_ISDIR(info.st_mode):
            folder_count += 1
        elif stat.S_ISREG(info.st_mode):
            file_count += 1
            total_bytes += info.st_size
        else:
            raise ValueError(f"Cannot delete a folder tree containing a special file: {child.name}")
    return file_count, folder_count, total_bytes


def current_staging_bytes() -> int:
    total = 0
    for path in STAGING_DIR.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


@app.before_request
def require_login():
    if request.endpoint in {"login", "static"}:
        return None
    if not authenticated():
        if request.path.startswith("/api/"):
            return jsonify(
                {"ok": False, "error": "Your web session expired. Refresh the page and sign in again."}
            ), 401
        return redirect(url_for("login", next=request.path))
    csrf_token()
    if request.method == 'POST' and request.endpoint not in {'logout', 'local_takeover'}:
        try:
            control = json.loads((STATE_DIR / 'fleet' / 'control.json').read_text())
        except FileNotFoundError:
            control = {}
        if control.get('mode') == 'managed' or (RUNTIME_DIR / 'takeover.request').exists():
            abort(409, 'Central manager owns this Pi. Request local takeover and wait for acknowledgment before editing.')
    return None


@app.post('/local-takeover')
def local_takeover():
    verify_csrf()
    request_file = RUNTIME_DIR / 'takeover.request'
    temporary = RUNTIME_DIR / ('.takeover-' + uuid.uuid4().hex)
    temporary.write_text(json.dumps({'request_id': str(uuid.uuid4()), 'operation': 'takeover'}))
    os.replace(temporary, request_file)
    flash('Local takeover requested. Wait until control mode shows local; any active operation must finish first.', 'success')
    return redirect(url_for('index'))


@app.context_processor
def template_values():
    return {"csrf_token": csrf_token, "human_size": human_size}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if secrets.compare_digest(username, WEB_USERNAME) and check_password_hash(PASSWORD_HASH, password):
            session.clear()
            session["authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            next_url = request.args.get("next", "")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("index")
            return redirect(next_url)
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    verify_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    folders = staged_folders()
    available_folders = {folder["path"] for folder in folders}
    selected_folder = request.args.get("folder", "").strip().replace("\\", "/")
    if selected_folder not in available_folders:
        selected_folder = ""
    try:
        fleet_control = json.loads((STATE_DIR / 'fleet' / 'control.json').read_text())
    except FileNotFoundError:
        fleet_control = {}
    return render_template(
        "index.html",
        fleet_control=fleet_control,
        files=staged_files(),
        folders=folders,
        managed_folders=[folder for folder in folders if folder["path"]],
        selected_folder=selected_folder,
        status=status_payload(),
        image_size_mib=IMAGE_SIZE_MIB,
        volume_label=piusb_ini.get("volume_label", "PIUSB"),
    )


def install_uploaded_files(uploads: list, selected_folder: str) -> list[dict]:
    """Validate and atomically install one or more uploaded files into staging.

    The traditional non-JavaScript form sends a batch and receives all-or-nothing
    behavior. The progress-monitor API calls this with one file per request, so
    each completed file is independently committed and reported to the browser.
    """
    if not uploads:
        raise ValueError("Choose at least one file to upload")

    with staging_lock(blocking=False):
        destination_dir = safe_existing_folder(selected_folder)
        transaction_dir = INCOMING_DIR / f"upload-{uuid.uuid4().hex}"
        transaction_dir.mkdir(parents=True, mode=0o700)
        pending: list[tuple[Path, Path, int]] = []
        seen_targets: set[Path] = set()
        seen_casefolded = existing_casefolded_paths()
        try:
            for index, upload_item in enumerate(uploads):
                raw_filename = upload_item.filename.replace("\\", "/").rsplit("/", 1)[-1]
                filename = secure_filename(raw_filename)
                if not filename:
                    raise ValueError(f"Upload {index + 1} has an invalid filename")
                validate_fat_component(filename)
                unresolved_target = destination_dir / filename
                if unresolved_target.is_symlink():
                    raise ValueError(f"Cannot replace a symbolic link: {filename}")
                target = unresolved_target.resolve()
                if not target.is_relative_to(STAGING_DIR):
                    raise ValueError("Upload target escaped the staging directory")
                if target in seen_targets:
                    raise ValueError(f"More than one uploaded file maps to {target.name}")
                seen_targets.add(target)
                if target.exists() and not target.is_file():
                    raise ValueError(f"Upload target is not a normal file: {filename}")
                folded_target = normalized_fat_path(target)
                existing_spelling = seen_casefolded.get(folded_target)
                target_spelling = target.relative_to(STAGING_DIR).as_posix()
                if existing_spelling is not None and existing_spelling != target_spelling:
                    raise ValueError(
                        "FAT32 is case-insensitive; the upload collides with "
                        f"{existing_spelling!r}"
                    )
                seen_casefolded[folded_target] = target_spelling

                temporary = transaction_dir / f"{index:04d}.upload"
                upload_item.save(temporary)
                size = temporary.stat().st_size
                if size > FAT32_MAX_FILE_SIZE:
                    raise ValueError(f"{filename} exceeds the FAT32 maximum file size")
                pending.append((temporary, target, size))

            projected = current_staging_bytes()
            for _, target, size in pending:
                if target.exists() and target.is_file():
                    projected -= target.stat().st_size
                projected += size
            if projected > STAGING_LIMIT_BYTES:
                raise ValueError(
                    f"The upload would use {human_size(projected)}, above the safe staging limit "
                    f"of {human_size(STAGING_LIMIT_BYTES)}"
                )

            installed_targets: list[Path] = []
            backups: list[tuple[Path, Path]] = []
            try:
                for index, (temporary, target, _) in enumerate(pending):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        backup = transaction_dir / f"{index:04d}.backup"
                        os.replace(target, backup)
                        backups.append((backup, target))
                    os.replace(temporary, target)
                    installed_targets.append(target)
            except Exception:
                for target in reversed(installed_targets):
                    target.unlink(missing_ok=True)
                for backup, target in reversed(backups):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
                raise

            records: list[dict] = []
            for target in installed_targets:
                info = target.stat()
                records.append(
                    {
                        "path": target.relative_to(STAGING_DIR).as_posix(),
                        "size": info.st_size,
                        "size_text": human_size(info.st_size),
                        "modified": time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(info.st_mtime)
                        ),
                    }
                )
            return records
        finally:
            shutil.rmtree(transaction_dir, ignore_errors=True)


@app.post("/upload")
def upload():
    verify_csrf()
    uploads = [item for item in request.files.getlist("files") if item and item.filename]
    selected_folder = request.form.get("folder", "").strip().replace("\\", "/")
    try:
        records = install_uploaded_files(uploads, selected_folder)
        flash(f"Uploaded {len(records)} file(s) to staging.", "success")
    except (ValueError, RuntimeError, OSError) as error:
        flash(str(error), "error")
    return redirect(url_for("index", folder=selected_folder))


@app.post("/api/uploads/file")
def api_upload_file():
    if not csrf_is_valid():
        return jsonify({"ok": False, "error": "Invalid CSRF token. Refresh the page and try again."}), 400

    selected_folder = request.form.get("folder", "").strip().replace("\\", "/")
    upload_item = request.files.get("file")
    if upload_item is None or not upload_item.filename:
        return jsonify({"ok": False, "error": "No file was supplied."}), 400

    try:
        records = install_uploaded_files([upload_item], selected_folder)
    except RuntimeError as error:
        return jsonify({"ok": False, "error": str(error)}), 409
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except OSError as error:
        app.logger.exception("Could not install uploaded file")
        return jsonify({"ok": False, "error": f"The Raspberry Pi could not store the file: {error}"}), 500

    return jsonify(
        {
            "ok": True,
            "file": records[0],
            "staging": staging_summary(),
        }
    ), 201


@app.post("/folders/create")
def create_folder():
    verify_csrf()
    parent_relative = request.form.get("parent_folder", "").strip().replace("\\", "/")
    raw_name = request.form.get("folder_name", "").strip()

    try:
        if not raw_name:
            raise ValueError("Enter a folder name")
        if "/" in raw_name or "\\" in raw_name:
            raise ValueError("Create one folder at a time; choose its parent from the folder picker")
        folder_name = secure_filename(raw_name)
        if not folder_name:
            raise ValueError("The folder name does not contain any usable characters")
        if len(folder_name.encode("utf-8")) > 240:
            raise ValueError("The normalized folder name is too long")
        validate_fat_component(folder_name)

        with staging_lock(blocking=False):
            parent = safe_existing_folder(parent_relative)
            folded_name = unicodedata.normalize("NFC", folder_name).casefold()
            for child in parent.iterdir():
                if unicodedata.normalize("NFC", child.name).casefold() == folded_name:
                    location = f"/{parent_relative}" if parent_relative else "the USB drive root"
                    raise ValueError(
                        "FAT32 is case-insensitive; an item with that name already exists in "
                        f"{location}"
                    )

            target = parent / folder_name
            target.mkdir(mode=0o750)
            relative_target = target.relative_to(STAGING_DIR).as_posix()
            flash(f"Created folder /{relative_target}.", "success")
            return redirect(url_for("index", folder=relative_target))
    except (ValueError, RuntimeError, OSError) as error:
        flash(str(error), "error")

    return redirect(url_for("index", folder=parent_relative))


@app.post("/delete")
def delete_file():
    verify_csrf()
    relative = request.form.get("path", "")
    try:
        with staging_lock(blocking=False):
            target = safe_staged_path(relative)
            if not target.is_file() or target.is_symlink():
                raise ValueError("The selected staged file no longer exists")
            target.unlink()
            flash(f"Deleted {relative} from staging.", "success")
    except (ValueError, RuntimeError, OSError) as error:
        flash(str(error), "error")
    return redirect(url_for("index"))


@app.post("/folders/delete")
def delete_folder():
    verify_csrf()
    relative = request.form.get("path", "").strip().replace("\\", "/")
    try:
        if not relative:
            raise ValueError("The USB drive root cannot be deleted")
        with staging_lock(blocking=False):
            target = safe_existing_folder(relative)
            if target == STAGING_DIR:
                raise ValueError("The USB drive root cannot be deleted")
            file_count, folder_count, total_bytes = directory_contents(target)
            shutil.rmtree(target)
            details = f"{file_count} file(s), {folder_count} subfolder(s), {human_size(total_bytes)}"
            flash(f"Deleted folder /{relative} and all of its contents ({details}).", "success")
    except (ValueError, RuntimeError, OSError) as error:
        flash(str(error), "error")
    return redirect(url_for("index"))


@app.get("/files/<path:relative>")
def download_file(relative: str):
    safe_staged_path(relative)
    return send_from_directory(STAGING_DIR, relative, as_attachment=True)


@app.post("/publish")
def request_publish():
    verify_csrf()
    status = read_status()
    if status.get("state") in {"building", "switching"} or PUBLISH_REQUEST.exists():
        flash("A publish is already queued or running.", "error")
        return redirect(url_for("index"))

    temporary = RUNTIME_DIR / f".publish.request-{os.getpid()}"
    try:
        temporary.write_text(f"requested_at={time.time()}\n", encoding="ascii")
        os.replace(temporary, PUBLISH_REQUEST)
        flash("Publish requested. The inactive image will be rebuilt before USB switches.", "success")
    except OSError as error:
        temporary.unlink(missing_ok=True)
        flash(f"Could not queue publish: {error}", "error")
    return redirect(url_for("index"))


@app.get("/api/status")
def api_status():
    return jsonify(status_payload())


@app.errorhandler(413)
def upload_too_large(_error):
    message = "The upload is larger than the configured image capacity."
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": message}), 413
    flash(message, "error")
    return redirect(url_for("index")), 303
