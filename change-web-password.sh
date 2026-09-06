#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run as root: sudo piusb-change-web-password" >&2
    exit 1
fi

while true; do
    read -r -s -p "New web password: " PASSWORD </dev/tty
    echo
    read -r -s -p "Confirm new web password: " CONFIRM </dev/tty
    echo
    if [[ -z ${PASSWORD} ]]; then
        echo "Password cannot be empty."
    elif [[ ${PASSWORD} != "${CONFIRM}" ]]; then
        echo "Passwords did not match."
    else
        break
    fi
done

PIUSB_PASSWORD="${PASSWORD}" python3 - <<'PY'
import configparser
import os
from pathlib import Path
from werkzeug.security import generate_password_hash

path = Path('/etc/piusb/web.ini')
parser = configparser.ConfigParser(interpolation=None)
if not parser.read(path):
    raise SystemExit(f'Cannot read {path}')
parser['web']['password_hash'] = generate_password_hash(os.environ['PIUSB_PASSWORD'])
temporary = path.with_suffix('.ini.tmp')
with temporary.open('w', encoding='utf-8') as handle:
    parser.write(handle)
os.chmod(temporary, 0o640)
os.chown(temporary, 0, __import__('grp').getgrnam('piusb').gr_gid)
os.replace(temporary, path)
PY
unset PASSWORD CONFIRM
systemctl restart piusb-web.service
echo "Web password changed."
