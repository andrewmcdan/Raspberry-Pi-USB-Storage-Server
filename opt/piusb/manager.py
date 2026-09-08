#!/usr/bin/env python3
"""Manage a double-buffered, file-backed USB mass-storage gadget.

The USB host is always given read-only access. Web uploads go to a staging
folder. Publishing rebuilds the inactive FAT32 image, disconnects the USB
gadget briefly, swaps the backing file, and reconnects it.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Stable module identity for the privileged bridge, including direct script execution.
sys.modules['piusb_publisher'] = sys.modules[__name__]

CONFIG_PATH = Path(os.environ.get("PIUSB_CONFIG", "/etc/piusb/piusb.ini"))
CONFIGFS_MOUNT = Path("/sys/kernel/config")
GADGETS_ROOT = CONFIGFS_MOUNT / "usb_gadget"
FAT32_MAX_FILE_SIZE = 4 * 1024**3 - 1
FORBIDDEN_FAT_CHARS = set('<>:"/\\|?*')
RESERVED_DOS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class Settings:
    image_dir: Path
    staging_dir: Path
    mount_dir: Path
    state_dir: Path
    runtime_dir: Path
    image_size_mib: int
    staging_fill_percent: int
    volume_label: str
    gadget_name: str
    manufacturer: str
    product: str
    serial: str
    vendor_id: str
    product_id: str
    bcd_device: str
    bcd_usb: str
    max_power: int
    bm_attributes: str
    stall: int
    disconnect_seconds: float
    udc: str

    @property
    def image_a(self) -> Path:
        return self.image_dir / "storage-a.img"

    @property
    def image_b(self) -> Path:
        return self.image_dir / "storage-b.img"

    @property
    def active_file(self) -> Path:
        return self.state_dir / "active"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def manager_lock(self) -> Path:
        return self.runtime_dir / "manager.lock"

    @property
    def staging_lock(self) -> Path:
        return self.runtime_dir / "staging.lock"

    @property
    def publish_request(self) -> Path:
        return self.runtime_dir / "publish.request"

    @property
    def gadget_path(self) -> Path:
        return GADGETS_ROOT / self.gadget_name

    @property
    def lun_path(self) -> Path:
        return self.gadget_path / "functions" / "mass_storage.usb0" / "lun.0"

    def image_for_slot(self, slot: str) -> Path:
        if slot == "A":
            return self.image_a
        if slot == "B":
            return self.image_b
        raise ValueError(f"Invalid image slot: {slot}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_settings() -> Settings:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(CONFIG_PATH):
        raise RuntimeError(f"Configuration file not found: {CONFIG_PATH}")
    section = parser["piusb"]

    label = section.get("volume_label", "PIUSB").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,11}", label):
        raise RuntimeError(
            "volume_label must contain 1 through 11 characters using A-Z, 0-9, underscore, or hyphen"
        )

    image_size_mib = section.getint("image_size_mib", 4096)
    if image_size_mib < 64:
        raise RuntimeError("image_size_mib must be at least 64")

    fill_percent = section.getint("staging_fill_percent", 90)
    if not 10 <= fill_percent <= 95:
        raise RuntimeError("staging_fill_percent must be between 10 and 95")

    stall = section.getint("stall", 0)
    if stall not in (0, 1):
        raise RuntimeError("stall must be 0 or 1")

    max_power = section.getint("max_power", 500)
    if not 0 <= max_power <= 500:
        raise RuntimeError("max_power must be between 0 and 500 mA for this USB 2.0 setup")

    disconnect_seconds = section.getfloat("disconnect_seconds", 3.0)
    if not 0.5 <= disconnect_seconds <= 60:
        raise RuntimeError("disconnect_seconds must be between 0.5 and 60")

    return Settings(
        image_dir=Path(section.get("image_dir", "/srv/piusb/images")),
        staging_dir=Path(section.get("staging_dir", "/srv/piusb/staging")),
        mount_dir=Path(section.get("mount_dir", "/mnt/piusb-build")),
        state_dir=Path(section.get("state_dir", "/var/lib/piusb")),
        runtime_dir=Path(section.get("runtime_dir", "/run/piusb")),
        image_size_mib=image_size_mib,
        staging_fill_percent=fill_percent,
        volume_label=label,
        gadget_name=section.get("gadget_name", "piusb").strip(),
        manufacturer=section.get("manufacturer", "Raspberry Pi").strip(),
        product=section.get("product", "Pi USB Publisher").strip(),
        serial=section.get("serial", "auto").strip(),
        vendor_id=section.get("vendor_id", "0x1d6b").strip(),
        product_id=section.get("product_id", "0x0104").strip(),
        bcd_device=section.get("bcd_device", "0x0100").strip(),
        bcd_usb=section.get("bcd_usb", "0x0200").strip(),
        max_power=max_power,
        bm_attributes=section.get("bm_attributes", "0x80").strip(),
        stall=stall,
        disconnect_seconds=disconnect_seconds,
        udc=section.get("udc", "auto").strip(),
    )


def require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("This command must run as root")


def ensure_directories(s: Settings) -> None:
    s.image_dir.mkdir(parents=True, exist_ok=True)
    s.staging_dir.mkdir(parents=True, exist_ok=True)
    s.mount_dir.mkdir(parents=True, exist_ok=True)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    s.runtime_dir.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_json(path: Path, value: dict) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def update_status(s: Settings, **changes: object) -> dict:
    status = read_json(s.status_file)
    status.update(changes)
    status["updated_at"] = utc_now()
    atomic_write_json(s.status_file, status)
    return status


def read_active_slot(s: Settings) -> str:
    try:
        slot = s.active_file.read_text(encoding="ascii").strip().upper()
    except FileNotFoundError:
        slot = "A"
    if slot not in {"A", "B"}:
        slot = "A"
    return slot


def write_active_slot(s: Settings, slot: str) -> None:
    if slot not in {"A", "B"}:
        raise ValueError(f"Invalid slot: {slot}")
    atomic_write_text(s.active_file, slot + "\n")


@contextlib.contextmanager
def exclusive_lock(path: Path, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), flags)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, check=check, text=True, capture_output=False)


def command_path(name: str) -> str:
    result = shutil.which(name)
    if not result:
        raise RuntimeError(f"Required command is not installed: {name}")
    return result


def is_mountpoint(path: Path) -> bool:
    result = subprocess.run(
        [command_path("mountpoint"), "-q", str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def validate_fat_component(name: str) -> None:
    if not name or name in {".", ".."}:
        raise RuntimeError(f"Invalid filename for FAT32: {name!r}")
    if name.endswith(" ") or name.endswith("."):
        raise RuntimeError(f"FAT32/Windows filename cannot end in a space or period: {name}")
    if any(character in FORBIDDEN_FAT_CHARS or ord(character) < 32 for character in name):
        raise RuntimeError(f"Filename contains a character not supported by FAT32/Windows: {name}")
    if len(name.encode("utf-16-le")) // 2 > 255:
        raise RuntimeError(f"Filename is longer than 255 UTF-16 characters: {name}")
    stem = name.split(".", 1)[0].rstrip(" .").upper()
    if stem in RESERVED_DOS_NAMES:
        raise RuntimeError(f"Filename is reserved by Windows: {name}")


def validate_staging(s: Settings) -> dict[str, object]:
    total_bytes = 0
    file_count = 0
    directory_count = 0
    manifest_records: list[str] = []
    casefolded_paths: dict[str, str] = {}

    for path in s.staging_dir.rglob("*"):
        relative = path.relative_to(s.staging_dir)
        relative_text = relative.as_posix()
        casefolded = unicodedata.normalize("NFC", relative_text).casefold()
        prior_spelling = casefolded_paths.get(casefolded)
        if prior_spelling is not None and prior_spelling != relative_text:
            raise RuntimeError(
                "FAT32 is case-insensitive, so these two paths collide: "
                f"{prior_spelling!r} and {relative_text!r}"
            )
        casefolded_paths[casefolded] = relative_text
        for component in relative.parts:
            validate_fat_component(component)

        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"Symbolic links are not supported in staging: {relative}")
        if stat.S_ISDIR(info.st_mode):
            directory_count += 1
            manifest_records.append(f"D\0{relative_text}")
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"Only normal files and directories are supported: {relative}")
        if info.st_size > FAT32_MAX_FILE_SIZE:
            raise RuntimeError(
                f"{relative} is too large for FAT32 ({info.st_size} bytes; maximum is {FAT32_MAX_FILE_SIZE})"
            )
        total_bytes += info.st_size
        file_count += 1
        manifest_records.append(
            f"F\0{relative_text}\0{info.st_size}\0{info.st_mtime_ns}"
        )

    image_bytes = s.image_size_mib * 1024**2
    conservative_limit = image_bytes * s.staging_fill_percent // 100
    if total_bytes > conservative_limit:
        raise RuntimeError(
            f"Staging contains {total_bytes} bytes, exceeding the configured safe limit "
            f"of {conservative_limit} bytes ({s.staging_fill_percent}% of the image)"
        )

    manifest = hashlib.sha256("\n".join(sorted(manifest_records)).encode("utf-8")).hexdigest()

    return {
        "total_bytes": total_bytes,
        "file_count": file_count,
        "directory_count": directory_count,
        "safe_limit_bytes": conservative_limit,
        "manifest": manifest,
    }


def build_image(s: Settings, target: Path) -> dict[str, object]:
    stats = validate_staging(s)
    building = target.with_name(target.name + ".building")

    if is_mountpoint(s.mount_dir):
        run([command_path("umount"), str(s.mount_dir)])

    building.unlink(missing_ok=True)
    target.unlink(missing_ok=True)

    try:
        run([command_path("truncate"), "-s", f"{s.image_size_mib}M", str(building)])
        volume_id = os.urandom(4).hex().upper()
        run([
            command_path("mkfs.vfat"),
            "-F", "32",
            "-n", s.volume_label,
            "-i", volume_id,
            str(building),
        ])

        s.mount_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(s.mount_dir, 0o700)
        run([command_path("mount"), "-o", "loop,rw,nodev,nosuid,noexec", str(building), str(s.mount_dir)])

        try:
            run([
                command_path("rsync"),
                "-rlt",
                "--delete",
                "--modify-window=1",
                "--no-perms",
                "--no-owner",
                "--no-group",
                "--safe-links",
                str(s.staging_dir) + "/",
                str(s.mount_dir) + "/",
            ])
            os.sync()
        finally:
            if is_mountpoint(s.mount_dir):
                run([command_path("umount"), str(s.mount_dir)])

        fsck = subprocess.run(
            [command_path("fsck.vfat"), "-a", str(building)],
            check=False,
            text=True,
        )
        if fsck.returncode not in (0, 1):
            raise RuntimeError(f"fsck.vfat failed with exit code {fsck.returncode}")

        os.replace(building, target)
        os.chmod(target, 0o640)
        os.sync()
        return stats
    except Exception:
        if is_mountpoint(s.mount_dir):
            subprocess.run([command_path("umount"), str(s.mount_dir)], check=False)
        building.unlink(missing_ok=True)
        raise


def write_attr(path: Path, value: str | int) -> None:
    with path.open("w", encoding="ascii") as handle:
        handle.write(f"{value}\n")


def read_attr(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return ""


def ensure_configfs() -> None:
    run([command_path("modprobe"), "dwc2"])
    run([command_path("modprobe"), "libcomposite"])
    CONFIGFS_MOUNT.mkdir(parents=True, exist_ok=True)
    if not is_mountpoint(CONFIGFS_MOUNT):
        run([command_path("mount"), "-t", "configfs", "none", str(CONFIGFS_MOUNT)])
    if not GADGETS_ROOT.exists():
        raise RuntimeError("USB gadget configfs path is unavailable after mounting configfs")


def get_serial(s: Settings) -> str:
    if s.serial.lower() != "auto":
        return s.serial
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="ascii", errors="ignore")
        match = re.search(r"^Serial\s*:\s*([0-9a-fA-F]+)\s*$", text, re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "0000000000000001"


def select_udc(s: Settings) -> str:
    deadline = time.monotonic() + 10.0
    available: list[str] = []
    while time.monotonic() < deadline:
        available = sorted(path.name for path in Path("/sys/class/udc").glob("*"))
        if available:
            break
        time.sleep(0.2)

    if not available:
        raise RuntimeError(
            "No USB Device Controller was found. Confirm dtoverlay=dwc2,dr_mode=peripheral and reboot."
        )
    if s.udc.lower() == "auto":
        return available[0]
    if s.udc not in available:
        raise RuntimeError(f"Configured UDC {s.udc!r} is not present. Available: {', '.join(available)}")
    return s.udc


def cleanup_gadget(s: Settings) -> None:
    gadget = s.gadget_path
    if not gadget.exists():
        return

    udc_file = gadget / "UDC"
    if udc_file.exists() and read_attr(udc_file):
        write_attr(udc_file, "")
        time.sleep(0.25)

    lun = gadget / "functions" / "mass_storage.usb0" / "lun.0"
    if (lun / "file").exists():
        try:
            write_attr(lun / "file", "")
        except OSError:
            pass

    config = gadget / "configs" / "c.1"
    if config.exists():
        for child in list(config.iterdir()):
            if child.is_symlink():
                child.unlink()
        strings = config / "strings" / "0x409"
        if strings.exists():
            strings.rmdir()
        strings_parent = config / "strings"
        if strings_parent.exists():
            strings_parent.rmdir()
        config.rmdir()

    configs = gadget / "configs"
    if configs.exists():
        configs.rmdir()

    function = gadget / "functions" / "mass_storage.usb0"
    if function.exists():
        function.rmdir()
    functions = gadget / "functions"
    if functions.exists():
        functions.rmdir()

    strings = gadget / "strings" / "0x409"
    if strings.exists():
        strings.rmdir()
    strings_parent = gadget / "strings"
    if strings_parent.exists():
        strings_parent.rmdir()

    os.rmdir(gadget)


def create_gadget(s: Settings, image: Path) -> None:
    if not image.is_file():
        raise RuntimeError(f"Backing image does not exist: {image}")

    ensure_configfs()
    cleanup_gadget(s)

    gadget = s.gadget_path
    gadget.mkdir()
    write_attr(gadget / "idVendor", s.vendor_id)
    write_attr(gadget / "idProduct", s.product_id)
    write_attr(gadget / "bcdDevice", s.bcd_device)
    write_attr(gadget / "bcdUSB", s.bcd_usb)

    strings = gadget / "strings" / "0x409"
    strings.mkdir(parents=True)
    write_attr(strings / "serialnumber", get_serial(s))
    write_attr(strings / "manufacturer", s.manufacturer)
    write_attr(strings / "product", s.product)

    config = gadget / "configs" / "c.1"
    config.mkdir(parents=True)
    write_attr(config / "MaxPower", s.max_power)
    write_attr(config / "bmAttributes", s.bm_attributes)
    config_strings = config / "strings" / "0x409"
    config_strings.mkdir(parents=True)
    write_attr(config_strings / "configuration", "Read-only published storage")

    function = gadget / "functions" / "mass_storage.usb0"
    function.mkdir(parents=True)
    write_attr(function / "stall", s.stall)
    lun = function / "lun.0"
    write_attr(lun / "ro", 1)
    write_attr(lun / "removable", 1)
    write_attr(lun / "cdrom", 0)
    write_attr(lun / "nofua", 0)
    write_attr(lun / "file", str(image.resolve()))

    (config / "mass_storage.usb0").symlink_to(function)
    write_attr(gadget / "UDC", select_udc(s))


def gadget_start(s: Settings) -> None:
    require_root()
    ensure_directories(s)
    with exclusive_lock(s.manager_lock):
        active = read_active_slot(s)
        image = s.image_for_slot(active)
        if not image.exists():
            fallback_slot = "B" if active == "A" else "A"
            fallback_image = s.image_for_slot(fallback_slot)
            if fallback_image.exists():
                write_active_slot(s, fallback_slot)
                update_status(
                    s,
                    state="starting",
                    message=(
                        f"Recorded image {active} was missing; using completed fallback image {fallback_slot}"
                    ),
                    active=fallback_slot,
                    target=None,
                    error=None,
                    active_build=None,
                    active_deployment=None,
                    prepared_deployment=None,
                    build=None,
                    candidate_build=None,
                )
                active = fallback_slot
                image = fallback_image
            else:
                update_status(
                    s,
                    state="building",
                    message=f"Both published images were unavailable; rebuilding image {active}",
                    active=active,
                    target=active,
                    started_at=utc_now(),
                    active_deployment=None,
                    prepared_deployment=None,
                )
                with exclusive_lock(s.staging_lock):
                    rebuilt_stats = build_image(s, image)
                update_status(s, active_build=rebuilt_stats, build=rebuilt_stats)
        create_gadget(s, image)
        update_status(
            s,
            state="idle",
            message=f"USB gadget is online using image {active}",
            active=active,
            target=None,
            error=None,
            gadget_online=True,
        )
        from fleet import reconcile
        reconcile(s)


def gadget_stop(s: Settings) -> None:
    require_root()
    ensure_directories(s)
    with exclusive_lock(s.manager_lock):
        ensure_configfs()
        cleanup_gadget(s)
        update_status(
            s,
            state="offline",
            message="USB gadget is offline",
            gadget_online=False,
            target=None,
        )


def swap_gadget_image(s: Settings, old_slot: str, new_slot: str) -> None:
    old_image = s.image_for_slot(old_slot)
    new_image = s.image_for_slot(new_slot)
    gadget = s.gadget_path

    if not gadget.exists():
        # Commit the valid, completed image before creating the gadget. If power
        # is lost here, the next boot will export the new slot.
        write_active_slot(s, new_slot)
        try:
            create_gadget(s, new_image)
        except Exception:
            write_active_slot(s, old_slot)
            raise
        return

    lun = s.lun_path
    udc_file = gadget / "UDC"
    prior_udc = read_attr(udc_file)

    try:
        forced_eject = lun / "forced_eject"
        if forced_eject.exists():
            try:
                write_attr(forced_eject, 1)
            except OSError:
                pass
        time.sleep(0.5)

        if prior_udc:
            write_attr(udc_file, "")
        time.sleep(max(0.5, s.disconnect_seconds))

        # Explicitly clear the prior backing-file association, select the
        # completed inactive image, then atomically commit its slot before USB
        # reconnects. This ordering gives deterministic recovery after power loss.
        write_attr(lun / "file", "")
        write_attr(lun / "file", str(new_image.resolve()))
        write_active_slot(s, new_slot)
        write_attr(udc_file, prior_udc or select_udc(s))
    except Exception:
        # Best-effort recovery to the image and state that were active before
        # publishing. Each recovery step is isolated so one failure does not
        # prevent the remaining steps from being attempted.
        recovery_errors: list[str] = []
        try:
            if read_attr(udc_file):
                write_attr(udc_file, "")
        except Exception as recovery_error:
            recovery_errors.append(f"unbind: {recovery_error}")
        try:
            write_attr(lun / "file", "")
            write_attr(lun / "file", str(old_image.resolve()))
        except Exception as recovery_error:
            recovery_errors.append(f"backing file: {recovery_error}")
        try:
            write_active_slot(s, old_slot)
        except Exception as recovery_error:
            recovery_errors.append(f"active state: {recovery_error}")
        try:
            write_attr(udc_file, prior_udc or select_udc(s))
        except Exception as recovery_error:
            recovery_errors.append(f"rebind: {recovery_error}")
        if recovery_errors:
            print("Recovery attempt had errors: " + "; ".join(recovery_errors), file=sys.stderr)
        raise


def publish(s: Settings) -> None:
    require_root()
    ensure_directories(s)
    s.publish_request.unlink(missing_ok=True)

    with exclusive_lock(s.manager_lock):
        from fleet import is_managed
        if is_managed(s):
            raise RuntimeError("Central manager owns publishing; request local takeover first")
        active = read_active_slot(s)
        target = "B" if active == "A" else "A"
        target_image = s.image_for_slot(target)
        started = utc_now()

        update_status(
            s,
            state="building",
            message=f"Building inactive image {target} from staging",
            active=active,
            target=target,
            error=None,
            started_at=started,
            gadget_online=s.gadget_path.exists() and bool(read_attr(s.gadget_path / "UDC")),
        )

        try:
            update_status(s, prepared_deployment=None)
            with exclusive_lock(s.staging_lock):
                stats = build_image(s, target_image)

            update_status(
                s,
                state="switching",
                message=(
                    f"Image {target} is complete. Disconnecting USB briefly and switching from {active} to {target}"
                ),
                active=active,
                target=target,
                candidate_build=stats,
            )

            update_status(s, active_deployment=None)
            swap_gadget_image(s, active, target)

            update_status(
                s,
                state="idle",
                message=f"Publish complete. USB is online using image {target}",
                active=target,
                target=None,
                error=None,
                gadget_online=True,
                completed_at=utc_now(),
                last_successful_publish=utc_now(),
                build=stats,
                active_build=stats,
                candidate_build=None,
                active_deployment=None,
            )
        except Exception as error:
            current_active = read_active_slot(s)
            recovery_message = (
                f"Publish failed; USB was restored to image {current_active}"
                if current_active == active
                else f"Publish failed; recorded active image is now {current_active}"
            )
            update_status(
                s,
                state="error",
                message=recovery_message,
                active=current_active,
                target=target,
                error=str(error),
                gadget_online=s.gadget_path.exists() and bool(read_attr(s.gadget_path / "UDC")),
                completed_at=utc_now(),
            )
            raise


def init_images(s: Settings) -> None:
    require_root()
    ensure_directories(s)
    with exclusive_lock(s.manager_lock):
        write_active_slot(s, "A")
        update_status(
            s,
            state="building",
            message="Creating initial image A",
            active="A",
            target="A",
            started_at=utc_now(),
            error=None,
            gadget_online=False,
        )
        with exclusive_lock(s.staging_lock):
            stats_a = build_image(s, s.image_a)
            update_status(
                s,
                state="building",
                message="Creating initial image B",
                active="A",
                target="B",
                build=stats_a,
            )
            stats_b = build_image(s, s.image_b)
        update_status(
            s,
            state="offline",
            message="Initial images created; reboot or start piusb-gadget.service",
            active="A",
            target=None,
            error=None,
            gadget_online=False,
            completed_at=utc_now(),
            build=stats_b,
            active_build=stats_a,
            candidate_build=None,
        )


def verify(s: Settings) -> int:
    checks: list[tuple[str, bool, str]] = []
    expected_size = s.image_size_mib * 1024**2

    try:
        recorded_active = s.active_file.read_text(encoding="ascii").strip().upper()
    except OSError:
        recorded_active = ""
    active_valid = recorded_active in {"A", "B"}
    active = recorded_active if active_valid else read_active_slot(s)
    expected_backing = s.image_for_slot(active)
    actual_backing_text = read_attr(s.lun_path / "file")
    try:
        backing_matches = bool(actual_backing_text) and Path(actual_backing_text).resolve() == expected_backing.resolve()
    except OSError:
        backing_matches = False

    checks.append(("Configuration", CONFIG_PATH.is_file(), str(CONFIG_PATH)))
    checks.append(("Staging directory", s.staging_dir.is_dir(), str(s.staging_dir)))
    checks.append((
        "Image A",
        s.image_a.is_file() and s.image_a.stat().st_size == expected_size,
        f"{s.image_a} ({s.image_a.stat().st_size if s.image_a.exists() else 0} bytes)",
    ))
    checks.append((
        "Image B",
        s.image_b.is_file() and s.image_b.stat().st_size == expected_size,
        f"{s.image_b} ({s.image_b.stat().st_size if s.image_b.exists() else 0} bytes)",
    ))
    checks.append(("Active state", active_valid, recorded_active or "missing"))
    checks.append(("UDC available", any(Path("/sys/class/udc").glob("*")), "/sys/class/udc"))
    checks.append(("Gadget configured", s.gadget_path.exists(), str(s.gadget_path)))
    checks.append(("Gadget bound", bool(read_attr(s.gadget_path / "UDC")), read_attr(s.gadget_path / "UDC") or "unbound"))
    checks.append(("LUN read-only", read_attr(s.lun_path / "ro") == "1", read_attr(s.lun_path / "ro") or "missing"))
    checks.append(("Backing image", backing_matches, actual_backing_text or "no media"))

    if Path("/run/systemd/system").exists() and shutil.which("systemctl"):
        for unit in ("piusb-gadget.service", "piusb-publish.path", "piusb-web.service"):
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            checks.append((unit, result.returncode == 0, "active" if result.returncode == 0 else "not active"))

    success = True
    for name, passed, detail in checks:
        print(f"{'OK' if passed else 'FAIL':4}  {name:24} {detail}")
        success &= passed
    print(json.dumps(read_json(s.status_file), indent=2, sort_keys=True))
    return 0 if success else 1


def show_status(s: Settings) -> None:
    payload = read_json(s.status_file)
    payload.setdefault("active", read_active_slot(s))
    payload["image_a"] = str(s.image_a)
    payload["image_b"] = str(s.image_b)
    payload["gadget_bound_udc"] = read_attr(s.gadget_path / "UDC")
    payload["gadget_backing_file"] = read_attr(s.lun_path / "file")
    print(json.dumps(payload, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["init-images", "gadget-start", "gadget-stop", "publish", "status", "verify", "fleet-request"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings()
    try:
        if args.command == "init-images":
            init_images(settings)
        elif args.command == "gadget-start":
            gadget_start(settings)
        elif args.command == "gadget-stop":
            gadget_stop(settings)
        elif args.command == "publish":
            publish(settings)
        elif args.command == "status":
            show_status(settings)
        elif args.command == "verify":
            return verify(settings)
        elif args.command == "fleet-request":
            from fleet import consume
            consume(settings)
        return 0
    except Exception as error:
        print(f"piusb-manager: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
