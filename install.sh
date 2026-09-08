#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer as root: sudo ./install.sh" >&2
    exit 1
fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_TXT=/boot/firmware/config.txt
GCODE_VIEWER=""
case "${1:-}" in
    --with-gcode-viewer) GCODE_VIEWER=true ;;
    --without-gcode-viewer) GCODE_VIEWER=false ;;
    "") ;;
    *) echo "Usage: sudo ./install.sh [--with-gcode-viewer|--without-gcode-viewer]" >&2; exit 1 ;;
esac
if (( $# > 1 )); then
    echo "Specify at most one installer option." >&2
    exit 1
fi

if [[ ! -f /etc/os-release || ! -f "${CONFIG_TXT}" ]]; then
    echo "This installer expects Raspberry Pi OS with ${CONFIG_TXT}." >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ ${ID:-} != "debian" && ${ID_LIKE:-} != *debian* ]]; then
    echo "Warning: this does not appear to be a Debian-family Raspberry Pi OS install." >&2
fi
if [[ ${VERSION_CODENAME:-unknown} != "trixie" ]]; then
    echo "Warning: this bundle was written for Raspberry Pi OS Trixie." >&2
    echo "Detected codename: ${VERSION_CODENAME:-unknown}" >&2
fi

MODEL="unknown Raspberry Pi"
if [[ -r /proc/device-tree/model ]]; then
    MODEL="$(tr -d '\0' </proc/device-tree/model)"
fi

echo "Detected hardware: ${MODEL}"
case "${MODEL}" in
    *"Zero"*|*"4 Model B"*|*"5 Model B"*)
        ;;
    *"3 Model A+"*)
        echo "Warning: Pi 3 Model A+ device mode requires unusual port/cable handling; this guide targets Pi Zero 2 W." >&2
        ;;
    *"Compute Module 4"*|*"Compute Module 5"*)
        echo "Warning: Compute Modules require carrier-board-specific USB device wiring and may need additional setup." >&2
        ;;
    *"3 Model B"*|*"3 Model B+"*)
        echo "Warning: Pi 3 Model B/B+ does not expose a supported USB device-mode port for this setup." >&2
        ;;
    *)
        echo "Warning: confirm this board exposes a USB device-capable controller before continuing." >&2
        ;;
esac
echo

DEFAULT_IMAGE_SIZE=4096
DEFAULT_VOLUME_LABEL=PIUSB
DEFAULT_WEB_USER=admin

read -r -p "Image size in MiB [${DEFAULT_IMAGE_SIZE}]: " IMAGE_SIZE_MIB </dev/tty || true
IMAGE_SIZE_MIB=${IMAGE_SIZE_MIB:-$DEFAULT_IMAGE_SIZE}
if [[ ! ${IMAGE_SIZE_MIB} =~ ^[0-9]+$ ]] || (( IMAGE_SIZE_MIB < 64 )); then
    echo "Image size must be an integer of at least 64 MiB." >&2
    exit 1
fi

read -r -p "FAT volume label, maximum 11 characters [${DEFAULT_VOLUME_LABEL}]: " VOLUME_LABEL </dev/tty || true
VOLUME_LABEL=${VOLUME_LABEL:-$DEFAULT_VOLUME_LABEL}
VOLUME_LABEL=${VOLUME_LABEL^^}
if [[ ! ${VOLUME_LABEL} =~ ^[A-Z0-9_-]{1,11}$ ]]; then
    echo "Volume label must contain 1 through 11 characters using A-Z, 0-9, underscore, or hyphen." >&2
    exit 1
fi

read -r -p "Web username [${DEFAULT_WEB_USER}]: " WEB_USERNAME </dev/tty || true
WEB_USERNAME=${WEB_USERNAME:-$DEFAULT_WEB_USER}
if [[ ! ${WEB_USERNAME} =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
    echo "Web username must contain 1 through 64 letters, numbers, periods, underscores, or hyphens." >&2
    exit 1
fi

while true; do
    read -r -s -p "Web password: " WEB_PASSWORD </dev/tty
    echo
    read -r -s -p "Confirm web password: " WEB_PASSWORD_CONFIRM </dev/tty
    echo
    if [[ -z ${WEB_PASSWORD} ]]; then
        echo "Password cannot be empty."
    elif [[ ${WEB_PASSWORD} != "${WEB_PASSWORD_CONFIRM}" ]]; then
        echo "Passwords did not match."
    else
        break
    fi
done
unset WEB_PASSWORD_CONFIRM

if [[ -z ${GCODE_VIEWER} ]]; then
    read -r -p "Install the optional G-code viewer for 3D printer files? [y/N]: " VIEWER_ANSWER </dev/tty || true
    case "${VIEWER_ANSWER:-n}" in
        [yY]|[yY][eE][sS]) GCODE_VIEWER=true ;;
        *) GCODE_VIEWER=false ;;
    esac
fi

echo "Installing required packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    avahi-daemon \
    dosfstools \
    gunicorn \
    python3 \
    python3-flask \
    rsync \
    util-linux

if command -v rpi-usb-gadget >/dev/null 2>&1; then
    # The official Trixie helper configures g_ether, which cannot own the UDC
    # at the same time as this custom mass-storage gadget.
    rpi-usb-gadget off >/dev/null 2>&1 || true
fi

systemctl stop piusb-web.service piusb-publish.path piusb-publish.service piusb-gadget.service 2>/dev/null || true
modprobe -r g_ether 2>/dev/null || true

if ! id piusb >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /opt/piusb --shell /usr/sbin/nologin piusb
fi

install -d -o root -g root -m 0755 /opt/piusb/web/templates
install -d -o root -g piusb -m 0750 /etc/piusb
install -d -o piusb -g piusb -m 0750 /srv/piusb/staging
install -d -o piusb -g piusb -m 0750 /srv/piusb/incoming
install -d -o root -g root -m 0750 /srv/piusb/images
install -d -o root -g piusb -m 0750 /var/lib/piusb
install -d -o root -g root -m 0700 /mnt/piusb-build
install -d -o piusb -g piusb -m 0770 /run/piusb
: > /run/piusb/staging.lock
chown piusb:piusb /run/piusb/staging.lock
chmod 0660 /run/piusb/staging.lock

install -o root -g root -m 0755 "${SOURCE_DIR}/opt/piusb/manager.py" /opt/piusb/manager.py
install -o root -g root -m 0644 "${SOURCE_DIR}/opt/piusb/fleet.py" /opt/piusb/fleet.py
install -o root -g root -m 0644 "${SOURCE_DIR}/opt/piusb/protocol.py" /opt/piusb/protocol.py
install -o root -g root -m 0644 "${SOURCE_DIR}/VERSION" /opt/piusb/VERSION
install -o root -g root -m 0755 "${SOURCE_DIR}/opt/piusb/web/app.py" /opt/piusb/web/app.py
install -o root -g root -m 0644 "${SOURCE_DIR}/opt/piusb/web/templates/login.html" /opt/piusb/web/templates/login.html
install -o root -g root -m 0644 "${SOURCE_DIR}/opt/piusb/web/templates/index.html" /opt/piusb/web/templates/index.html
if [[ ${GCODE_VIEWER} == true ]]; then
    # Shared file list keeps fresh installs and in-place upgrades identical.
    /usr/bin/python3 "${SOURCE_DIR}/scripts/upgrade_gcode_viewer.py" --fresh-assets
fi

install -o root -g piusb -m 0640 "${SOURCE_DIR}/etc/piusb/piusb.ini" /etc/piusb/piusb.ini
sed -i -E "s/^image_size_mib[[:space:]]*=.*/image_size_mib = ${IMAGE_SIZE_MIB}/" /etc/piusb/piusb.ini
sed -i -E "s/^volume_label[[:space:]]*=.*/volume_label = ${VOLUME_LABEL}/" /etc/piusb/piusb.ini

PASSWORD_HASH="$({ PIUSB_PASSWORD="${WEB_PASSWORD}" python3 - <<'PY'
import os
from werkzeug.security import generate_password_hash
print(generate_password_hash(os.environ["PIUSB_PASSWORD"]))
PY
} )"
unset WEB_PASSWORD
SECRET_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"

cat >/etc/piusb/web.ini <<EOF
[web]
username = ${WEB_USERNAME}
password_hash = ${PASSWORD_HASH}
secret_key = ${SECRET_KEY}
gcode_viewer = ${GCODE_VIEWER}
EOF
chown root:piusb /etc/piusb/web.ini
chmod 0640 /etc/piusb/web.ini

if [[ ! -e /srv/piusb/staging/README.txt ]] && [[ -z "$(find /srv/piusb/staging -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    cat >/srv/piusb/staging/README.txt <<'EOF'
Pi USB Publisher
================

Files uploaded through the web interface are placed in a staging folder on the
Raspberry Pi. They appear on this USB drive only after Build and switch USB
image is selected in the web interface.

The USB drive is intentionally read-only to the connected host.
EOF
    chown piusb:piusb /srv/piusb/staging/README.txt
    chmod 0640 /srv/piusb/staging/README.txt
fi

# Remove a previous installer-owned block and any exact standalone duplicate.
sed -i '/^# BEGIN PIUSB PUBLISHER$/,/^# END PIUSB PUBLISHER$/d' "${CONFIG_TXT}"
sed -i '/^dtoverlay=dwc2,dr_mode=peripheral$/d' "${CONFIG_TXT}"
cat >>"${CONFIG_TXT}" <<'EOF'

# BEGIN PIUSB PUBLISHER
[all]
dtoverlay=dwc2,dr_mode=peripheral
# END PIUSB PUBLISHER
EOF

cat >/etc/modules-load.d/piusb.conf <<'EOF'
dwc2
libcomposite
EOF

install -o root -g root -m 0644 "${SOURCE_DIR}/etc/tmpfiles.d/piusb.conf" /etc/tmpfiles.d/piusb.conf
install -o root -g root -m 0644 "${SOURCE_DIR}/etc/systemd/system/piusb-gadget.service" /etc/systemd/system/piusb-gadget.service
install -o root -g root -m 0644 "${SOURCE_DIR}/etc/systemd/system/piusb-publish.service" /etc/systemd/system/piusb-publish.service
install -o root -g root -m 0644 "${SOURCE_DIR}/etc/systemd/system/piusb-publish.path" /etc/systemd/system/piusb-publish.path
install -o root -g root -m 0644 "${SOURCE_DIR}/etc/systemd/system/piusb-web.service" /etc/systemd/system/piusb-web.service

install -o root -g root -m 0755 "${SOURCE_DIR}/piusbctl" /usr/local/sbin/piusbctl
install -o root -g root -m 0755 "${SOURCE_DIR}/change-web-password.sh" /usr/local/sbin/piusb-change-web-password

systemd-tmpfiles --create /etc/tmpfiles.d/piusb.conf
chown piusb:piusb /run/piusb /run/piusb/staging.lock
chmod 0770 /run/piusb
chmod 0660 /run/piusb/staging.lock

# The two active/inactive images plus staging can eventually consume roughly
# three times the staged data, so warn about obviously small filesystems.
AVAILABLE_MIB=$(df --output=avail -BM /srv/piusb | tail -1 | tr -dc '0-9')
RECOMMENDED_MIB=$(( IMAGE_SIZE_MIB * 3 + 4096 ))
if [[ -n ${AVAILABLE_MIB} ]] && (( AVAILABLE_MIB < RECOMMENDED_MIB )); then
    echo
    echo "Warning: /srv has about ${AVAILABLE_MIB} MiB free."
    echo "A ${IMAGE_SIZE_MIB} MiB image configuration can require about ${RECOMMENDED_MIB} MiB"
    echo "for staging, both populated images, and working space. A larger SD card may be needed."
    echo
fi

echo "Creating the initial A and B images..."
/usr/bin/python3 /opt/piusb/manager.py init-images

systemctl daemon-reload
systemctl enable piusb-gadget.service piusb-publish.path piusb-web.service avahi-daemon.service

echo
echo "Installation complete. For a Pi Zero 2 W, shut down and move from PWR IN"
echo "to the USB data port so the next boot activates peripheral mode:"
echo "  sudo poweroff"
echo
echo "After the Pi boots from the host USB connection, open:"
echo "  http://$(hostname).local:8080"
echo "Optional G-code viewer enabled: ${GCODE_VIEWER}"
echo
echo "Useful checks:"
echo "  sudo piusbctl verify"
echo "  sudo piusbctl status"
echo "  sudo journalctl -u piusb-gadget -u piusb-publish -u piusb-web -n 100 --no-pager"
