import json
import subprocess

import pytest

from wifi_watchdog import Watchdog, usable_address

PROFILE = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


class Network:
    def __init__(self):
        self.state, self.address = 100, '10.0.0.2'
        self.calls, self.fail = [], False

    def run(self, args, timeout):
        self.calls.append(args)
        assert timeout <= 125
        if args[0] == 'ip':
            return json.dumps([{'addr_info': [{'scope': 'global', 'local': self.address}]}])
        args = args[3:]  # nmcli --wait N
        if args[:2] == ['-g', 'GENERAL.STATE']:
            return f'{self.state} (state)'
        if args[:2] in (['-g', 'GENERAL.CON-UUID'], ['-g', 'UUID']):
            return PROFILE
        if args[:2] == ['-g', 'connection.type,connection.interface-name,connection.autoconnect,connection.autoconnect-priority']:
            return '802-11-wireless\n\nyes\n0'
        if args[:2] == ['connection', 'up']:
            if self.fail:
                raise RuntimeError('AP unavailable')
            self.state = 100
        return ''


def setup(tmp_path, network, **kwargs):
    return Watchdog(state_file=tmp_path / 'profile.json', run=network.run, **kwargs)


def test_healthy_connection_untouched_and_remembered(tmp_path):
    network = Network()
    watchdog = setup(tmp_path, network)
    assert watchdog.step() == 'connected'
    assert all(c[0] == 'ip' or c[3] == '-g' for c in network.calls)
    assert json.loads((tmp_path / 'profile.json').read_text())['uuid'] == PROFILE
    assert setup(tmp_path, network).last_profile == PROFILE


def test_saved_profile_reconnects_without_new_network(tmp_path):
    network = Network()
    network.state = 30
    watchdog = setup(tmp_path, network)
    assert watchdog.step() == 'retrying'
    assert ['nmcli', '--wait', '25', 'connection', 'up', 'uuid', PROFILE, 'ifname', 'wlan0'] in network.calls
    assert watchdog.step() == 'connected'
    assert watchdog.failures == 0
    assert not any(c[0] == 'systemctl' or 'reboot' in c or 'connect' in c for c in network.calls)


def test_retries_indefinitely_and_resets_only_after_failures(tmp_path):
    network = Network()
    network.state, network.fail = 30, True
    watchdog = setup(tmp_path, network)
    for _ in range(20):
        with pytest.raises(RuntimeError, match='AP unavailable'):
            watchdog.step()
    assert watchdog.failures == 20
    resets = [c for c in network.calls if c[3:5] == ['device', 'disconnect']]
    assert len(resets) == 3
    network.fail = False
    watchdog.step()
    assert watchdog.step() == 'connected'


def test_dhcp_gets_grace_then_stalled_activation_retried(tmp_path):
    network = Network()
    network.state = 70
    now = [0]
    watchdog = setup(tmp_path, network, clock=lambda: now[0])
    assert watchdog.step() == 'connecting'
    now[0] = 89
    assert watchdog.step() == 'connecting'
    now[0] = 90
    assert watchdog.step() == 'retrying'


def test_race_with_networkmanager_recovery(tmp_path, monkeypatch):
    network = Network()
    watchdog = setup(tmp_path, network)
    health = iter([(30, False), (100, True)])
    monkeypatch.setattr(watchdog, 'health', lambda: next(health))
    assert watchdog.step() == 'connected'
    assert not any(c[3:5] == ['connection', 'up'] for c in network.calls)


def test_unmanaged_device_not_taken_over(tmp_path):
    network = Network()
    network.state = 10
    assert setup(tmp_path, network).step() == 'unmanaged'
    assert len(network.calls) == 1


@pytest.mark.parametrize('address,valid', [('169.254.1.2', False), ('fe80::1', False),
                                        ('127.0.0.1', False), ('0.0.0.0', False),
                                        ('10.0.0.2', True), ('fd00::1', True)])
def test_usable_lan_address(address, valid):
    assert usable_address(json.dumps([{'addr_info': [{'scope': 'global', 'local': address}]}])) is valid


def test_no_saved_profiles_keeps_monitoring(tmp_path):
    network = Network()
    network.state = 30
    watchdog = setup(tmp_path, network)
    watchdog.profiles = lambda: []
    assert watchdog.step() == 'no-profile'
    assert watchdog.step() == 'no-profile'


def test_monitor_survives_command_timeout(tmp_path, monkeypatch):
    network = Network()
    watchdog = setup(tmp_path, network)
    calls = []
    def step():
        calls.append(1)
        raise subprocess.TimeoutExpired('nmcli', 10)
    def sleep(_seconds):
        if len(calls) == 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(watchdog, 'step', step)
    monkeypatch.setattr('wifi_watchdog.time.sleep', sleep)
    with pytest.raises(KeyboardInterrupt):
        watchdog.serve()
    assert len(calls) == 3
