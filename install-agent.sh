#!/usr/bin/env bash
# Non-destructive upgrade: never invokes init-images or changes existing credentials.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo'; exit 1; }
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f /etc/piusb/piusb.ini ]] || { echo 'Install Pi USB Publisher first'; exit 1; }
id piusb >/dev/null
python3 - <<'PY'
import configparser
parser = configparser.ConfigParser()
parser.read('/etc/piusb/piusb.ini')
expected = {'staging_dir': '/srv/piusb/staging', 'state_dir': '/var/lib/piusb', 'runtime_dir': '/run/piusb'}
for key, value in expected.items():
    if parser['piusb'].get(key, value) != value:
        raise SystemExit('Custom data paths require corresponding agent systemd sandbox changes before installation: ' + key)
PY
apt-get update
apt-get install -y python3-requests
bash "${SOURCE_DIR}/install-wifi-watchdog.sh"
# Drain running operations before replacing their Python code.
systemctl stop piusb-agent.service 2>/dev/null || true
systemctl stop piusb-publish.path
systemctl stop piusb-fleet.path 2>/dev/null || true
busy() {
    local state
    state=$(systemctl show --property=ActiveState --value "$1" 2>/dev/null || true)
    [[ $state == active || $state == activating || $state == deactivating ]]
}
while busy piusb-publish.service || busy piusb-fleet.service; do sleep 2; done
systemctl stop piusb-web.service
install -d -o piusb -g piusb -m 0750 /srv/piusb/agent
for file in manager.py protocol.py fleet.py agent.py pi_export.py pi_import.py; do
    install -o root -g root -m 0755 "${SOURCE_DIR}/opt/piusb/${file}" "/opt/piusb/${file}"
done
install -o root -g root -m 0644 "${SOURCE_DIR}/opt/piusb/web/app.py" /opt/piusb/web/app.py
install -o root -g root -m 0644 "${SOURCE_DIR}/opt/piusb/web/templates/index.html" /opt/piusb/web/templates/index.html
for unit in piusb-agent.service piusb-fleet.service piusb-fleet.path; do
    install -o root -g root -m 0644 "${SOURCE_DIR}/etc/systemd/system/${unit}" "/etc/systemd/system/${unit}"
done
install -o root -g root -m 0644 "${SOURCE_DIR}/VERSION" /opt/piusb/VERSION
install -o root -g root -m 0644 "${SOURCE_DIR}/opt/piusb/web/templates/login.html" /opt/piusb/web/templates/login.html
# Update optional viewer assets while preserving its configured enabled state.
python3 "${SOURCE_DIR}/scripts/upgrade_gcode_viewer.py" --fresh-assets
systemctl daemon-reload
systemctl enable --now piusb-fleet.path piusb-agent.service
systemctl start piusb-publish.path piusb-web.service
echo 'Agent installed. Existing images, staging, USB binding and web credentials retained.'
echo 'Create an enrollment token in the manager and run the displayed command on this Pi.'
