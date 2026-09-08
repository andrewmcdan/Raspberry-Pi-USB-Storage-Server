# Automatic Wi-Fi recovery

`piusb-wifi-watchdog.service` monitors the Pi's NetworkManager Wi-Fi connection
every 30 seconds. It reconnects indefinitely after AP outages using existing
saved profiles and credentials. It does not reboot the Pi, restart NetworkManager,
touch USB images, or restart publishing services.

New publisher and agent installations include it. To add **only** Wi-Fi recovery
to an existing Pi, copy this repository and run:

```bash
sudo bash ./install-wifi-watchdog.sh
sudo systemctl status piusb-wifi-watchdog.service
sudo journalctl -u piusb-wifi-watchdog.service -n 50 --no-pager
```

The standalone installer preserves an existing watchdog configuration and does
not change NetworkManager profiles. It requires NetworkManager and `iproute2`,
as supplied by the project's Raspberry Pi OS target. It starts immediately and
is enabled for subsequent boots; it does not wait for `network-online.target`,
so it can recover a Pi that boots while the AP is unavailable.

Recovery behavior:

- A connection is healthy when NetworkManager reports connected and the interface
  has a usable IPv4 or IPv6 address. Link-local-only addresses are insufficient.
- The watchdog remembers the last healthy connection UUID in
  `/var/lib/piusb-wifi/profile.json`. With `connection_uuid = auto`, it tries that
  saved profile first, then compatible saved autoconnect profiles in priority
  order. It never creates a profile or joins an unknown open network.
- Activating/authenticating/DHCP connections get 90 seconds to complete. Each
  explicit connection attempt is bounded to 25 seconds; unsuccessful attempts
  are followed by another monitor interval. There is no maximum retry count.
- After every six unsuccessful attempts, it disconnects the stalled Wi-Fi
  interface before retrying the saved profile. It rechecks health before taking
  recovery action to allow NetworkManager's normal recovery to finish first.
- NetworkManager/command errors are logged and retried. A crashed watchdog is
  restarted by systemd. An unmanaged interface is reported but not taken over.

Settings live in `/etc/piusb/wifi-watchdog.ini`:

```ini
[wifi]
interface = wlan0
connection_uuid = auto
interval_seconds = 30
connect_timeout_seconds = 25
activation_grace_seconds = 90
reset_after_attempts = 6
```

Set `connection_uuid` to a saved profile UUID to restrict reconnection to one
network (`nmcli -g GENERAL.CON-UUID device show wlan0` displays the active UUID).
Restart the watchdog after configuration changes. UUIDs and metadata are stored;
Wi-Fi passwords remain in NetworkManager and are not copied or logged.

The health check deliberately does not ping an internet service or the manager:
an internet/server outage should not cause an otherwise working LAN connection
to be reset. It confirms local Wi-Fi/IP connectivity, not internet reachability.
For intentional Wi-Fi maintenance, stop the watchdog first; otherwise it will
turn the Wi-Fi radio back on and reconnect:

```bash
sudo systemctl stop piusb-wifi-watchdog.service
# Perform maintenance, then resume recovery:
sudo systemctl start piusb-wifi-watchdog.service
```

For a read-only check:

```bash
sudo python3 /opt/piusb/wifi_watchdog.py --check
```

The command and profile semantics follow the
[NetworkManager nmcli manual](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli.html).

## Verified on 2026-09-08

The service was installed and enabled on the available Pi running NetworkManager
1.52.1. A scheduled `nmcli device disconnect wlan0` deliberately dropped Wi-Fi.
The watchdog logged its first saved-profile reconnect attempt on the next poll;
SSH returned and the following poll confirmed connected/IP-ready state. An
independent 120-second fallback had execution timestamp zero and was canceled.
The original USB backing image was unchanged and all publisher services remained
active. The systemd unit passed `systemd-analyze verify` on the Pi.

Fourteen watchdog tests cover healthy-link preservation, persistent profile
selection, continuous failures and recovery, interface reset cadence, DHCP grace,
NetworkManager recovery races, unmanaged devices, missing profiles, IPv4/IPv6
address handling, and command timeouts. The combined repository suite passed
49 tests. A full AP reboot and a week-long outage soak were not performed.
