#!/usr/bin/env python3
"""Continuously restore a saved NetworkManager Wi-Fi connection."""
import argparse
import configparser
import ipaddress
import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

LOG = logging.getLogger('piusb-wifi')


def command(args, timeout=15):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL': 'C'}, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr.strip()}')
    return result.stdout.strip()


def usable_address(encoded):
    for device in json.loads(encoded):
        for entry in device.get('addr_info', []):
            if entry.get('scope') != 'global' or entry.get('tentative') or entry.get('dadfailed'):
                continue
            address = ipaddress.ip_address(entry['local'])
            if not (address.is_link_local or address.is_loopback or address.is_unspecified):
                return True
    return False


class Watchdog:
    def __init__(self, interface='wlan0', profile='auto', interval=30, connect_timeout=25,
                 grace=90, reset_after=6, state_file=Path('/var/lib/piusb-wifi/profile.json'),
                 run=command, clock=time.monotonic):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', interface):
            raise ValueError('Invalid Wi-Fi interface name')
        if profile != 'auto':
            profile = str(uuid.UUID(profile))
        if interval < 5 or not 5 <= connect_timeout <= 120 or grace < connect_timeout or reset_after < 1:
            raise ValueError('Invalid watchdog timing configuration')
        self.interface, self.profile = interface, profile
        self.interval, self.connect_timeout, self.grace = interval, connect_timeout, grace
        self.reset_after, self.state_file = reset_after, Path(state_file)
        self.run, self.clock = run, clock
        self.failures, self.connecting_since, self.last_message = 0, None, None
        self.last_profile = None
        try:
            saved = json.loads(self.state_file.read_text())
            if saved.get('interface') == interface:
                self.last_profile = str(uuid.UUID(saved['uuid']))
        except (OSError, ValueError, KeyError):
            pass

    def nm(self, *args, wait=10):
        return self.run(['nmcli', '--wait', str(wait), *args], timeout=wait + 5)

    def say(self, message):
        if message != self.last_message:
            LOG.info(message)
            self.last_message = message

    def health(self):
        # Numeric state is locale-independent; the human-readable suffix is ignored.
        state = int(self.nm('-g', 'GENERAL.STATE', 'device', 'show', self.interface).split()[0])
        ready = state == 100 and usable_address(self.run(['ip', '-j', 'address', 'show', 'dev', self.interface], timeout=10))
        return state, ready

    def remember(self):
        if self.profile != 'auto':
            return
        current = str(uuid.UUID(self.nm('-g', 'GENERAL.CON-UUID', 'device', 'show', self.interface)))
        if current == self.last_profile:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump({'interface': self.interface, 'uuid': current}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_file)
        self.last_profile = current

    def profiles(self):
        if self.profile != 'auto':
            candidates = [self.profile]
        else:
            candidates = self.nm('-g', 'UUID', 'connection', 'show').splitlines()
        eligible = []
        for candidate in candidates:
            candidate = str(uuid.UUID(candidate))
            # Inspect metadata only, never Wi-Fi passwords. Explicit profiles must also be Wi-Fi.
            details = self.nm('-g', 'connection.type,connection.interface-name,connection.autoconnect,connection.autoconnect-priority',
                              'connection', 'show', 'uuid', candidate).splitlines()
            if len(details) != 4:
                continue
            kind, interface, autoconnect, priority = details
            if kind != '802-11-wireless' or interface not in ('', '--', self.interface):
                continue
            if self.profile == 'auto' and autoconnect != 'yes' and candidate != self.last_profile:
                continue
            eligible.append((candidate == self.last_profile, int(priority), candidate))
        return [row[2] for row in sorted(eligible, reverse=True)]

    def step(self):
        state, ready = self.health()
        if ready:
            self.remember()
            self.failures, self.connecting_since = 0, None
            self.say(f'{self.interface}: Wi-Fi connected with a usable IP address')
            return 'connected'
        if state == 10:
            self.say(f'{self.interface}: unmanaged; configure this interface in NetworkManager')
            return 'unmanaged'
        if 40 <= state <= 90:
            if self.connecting_since is None:
                self.connecting_since = self.clock()
            if self.clock() - self.connecting_since < self.grace:
                self.say(f'{self.interface}: waiting for NetworkManager activation/DHCP')
                return 'connecting'
        else:
            self.connecting_since = None
        profiles = self.profiles()
        if not profiles:
            self.say(f'{self.interface}: no compatible saved Wi-Fi profile; will keep checking')
            return 'no-profile'
        # Recheck immediately before recovery so normal NetworkManager recovery wins.
        if self.health()[1]:
            return 'connected'
        profile = profiles[self.failures % len(profiles)]
        attempt = self.failures + 1
        self.last_message = None
        LOG.info('%s: reconnect attempt %d using saved profile %s', self.interface, attempt, profile)
        try:
            self.nm('radio', 'wifi', 'on')
            if self.failures and self.failures % self.reset_after == 0:
                if self.health()[1]:
                    return 'connected'
                LOG.warning('%s: resetting stalled Wi-Fi connection before retry', self.interface)
                try:
                    self.nm('device', 'disconnect', self.interface)
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    LOG.warning('Disconnect/reset: %s', error)
            self.nm('connection', 'up', 'uuid', profile, 'ifname', self.interface, wait=self.connect_timeout)
        finally:
            # Count even timed-out attempts. A healthy poll resets this counter.
            self.failures += 1
            self.connecting_since = self.clock()
        return 'retrying'

    def serve(self):
        while True:
            try:
                self.step()
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                LOG.warning('%s: %s; retrying in %ss', self.interface, error, self.interval)
            time.sleep(self.interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/piusb/wifi-watchdog.ini')
    parser.add_argument('--check', action='store_true', help='Read-only connection check; never reconnects')
    args = parser.parse_args()
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(args.config):
        parser.error('Watchdog configuration not found')
    section = config['wifi']
    watchdog = Watchdog(interface=section.get('interface', 'wlan0'), profile=section.get('connection_uuid', 'auto'),
                        interval=section.getint('interval_seconds', 30), connect_timeout=section.getint('connect_timeout_seconds', 25),
                        grace=section.getint('activation_grace_seconds', 90), reset_after=section.getint('reset_after_attempts', 6))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.check:
        state, ready = watchdog.health()
        print(json.dumps({'interface': watchdog.interface, 'networkmanager_state': state, 'connected': ready}))
        return 0 if ready else 1
    watchdog.serve()


if __name__ == '__main__':
    raise SystemExit(main())
