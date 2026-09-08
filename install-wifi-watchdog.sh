#!/usr/bin/env bash
# Install only Wi-Fi recovery; no publisher, USB, or profile changes.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo'; exit 1; }
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v nmcli >/dev/null || { echo 'NetworkManager/nmcli is required'; exit 1; }
command -v ip >/dev/null || { echo 'iproute2 is required'; exit 1; }
systemctl is-active --quiet NetworkManager.service || { echo 'NetworkManager must be running'; exit 1; }
for directory in /opt/piusb /etc/piusb; do
    if [[ ! -d $directory ]]; then
        install -d -o root -g root -m 0755 "$directory"
    fi
done
install -o root -g root -m 0755 "${SOURCE_DIR}/opt/piusb/wifi_watchdog.py" /opt/piusb/wifi_watchdog.py
if [[ ! -f /etc/piusb/wifi-watchdog.ini ]]; then
    install -o root -g root -m 0644 "${SOURCE_DIR}/etc/piusb/wifi-watchdog.ini" /etc/piusb/wifi-watchdog.ini
fi
install -o root -g root -m 0644 "${SOURCE_DIR}/etc/systemd/system/piusb-wifi-watchdog.service" /etc/systemd/system/piusb-wifi-watchdog.service
systemctl daemon-reload
systemctl enable piusb-wifi-watchdog.service
systemctl restart piusb-wifi-watchdog.service
echo 'Wi-Fi watchdog enabled. Saved network credentials, USB images and publisher services retained.'
