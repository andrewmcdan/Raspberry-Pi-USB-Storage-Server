import dataclasses
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / 'opt' / 'piusb'))
spec = importlib.util.spec_from_file_location('piusb_publisher', Path(__file__).resolve().parents[1] / 'opt/piusb/manager.py')
publisher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = publisher
spec.loader.exec_module(publisher)
import fleet
from protocol import digest_file, manifest_hash


@pytest.fixture
def hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, 'CONFIG_PATH', Path(__file__).resolve().parents[1] / 'etc/piusb/piusb.ini')
    s = dataclasses.replace(publisher.load_settings(), image_dir=tmp_path / 'srv/images',
                            staging_dir=tmp_path / 'srv/staging', state_dir=tmp_path / 'state',
                            runtime_dir=tmp_path / 'run', mount_dir=tmp_path / 'mount', image_size_mib=64)
    monkeypatch.setattr(publisher, 'GADGETS_ROOT', tmp_path / 'gadgets')
    monkeypatch.setattr(publisher, 'require_root', lambda: None)
    publisher.ensure_directories(s)
    s.lun_path.mkdir(parents=True)
    (s.lun_path / 'ro').write_text('1')
    (s.gadget_path / 'UDC').write_text('dummy')
    (s.lun_path / 'file').write_text(str(s.image_a.resolve()))
    publisher.write_active_slot(s, 'A')
    s.image_a.write_bytes(b'old image')
    calls = []

    def build(settings, target):
        publisher.validate_staging(settings)
        target.write_bytes(b'verified image')
        calls.append('build')
        return {'total_bytes': 5}

    def swap(settings, old, new):
        calls.append('swap')
        publisher.write_active_slot(settings, new)
        (settings.lun_path / 'file').write_text(str(settings.image_for_slot(new).resolve()))

    monkeypatch.setattr(publisher, 'build_image', build)
    monkeypatch.setattr(publisher, 'swap_gadget_image', swap)
    return s, calls


def candidate(s):
    job = str(uuid.uuid4())
    root = s.staging_dir.parent / 'agent/candidates' / job
    (root / 'files').mkdir(parents=True)
    content = root / 'files/hello.txt'
    content.write_bytes(b'hello')
    manifest = [{'path': 'hello.txt', 'kind': 'file', 'size': 5, 'sha256': digest_file(content)}]
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return {'deployment': job, 'operation': 'prepare', 'request_id': str(uuid.uuid4()), 'hash': manifest_hash(manifest)}


def test_prepare_hold_activate_and_duplicate(hardware):
    s, calls = hardware
    request = candidate(s)
    result = fleet.process(s, request)
    assert result['state'] == 'prepared' and calls == ['build']
    assert publisher.read_active_slot(s) == 'A'
    assert fleet.process(s, request)['state'] == 'prepared'
    request['operation'] = 'activate'
    assert fleet.process(s, request)['state'] == 'succeeded'
    assert fleet.process(s, request)['state'] == 'succeeded'
    assert calls == ['build', 'swap']


def test_hash_failure_keeps_active(hardware):
    s, calls = hardware
    request = candidate(s)
    (s.staging_dir.parent / 'agent/candidates' / request['deployment'] / 'files/hello.txt').write_bytes(b'wrong')
    result = fleet.process(s, request)
    assert result['state'] == 'failed' and not calls
    assert s.image_a.read_bytes() == b'old image'


def test_symlink_rejected(hardware):
    s, calls = hardware
    request = candidate(s)
    path = s.staging_dir.parent / 'agent/candidates' / request['deployment'] / 'files/hello.txt'
    path.unlink()
    path.symlink_to(s.image_a)
    assert fleet.process(s, request)['state'] == 'failed'
    assert not calls


def test_reconcile_after_lost_activation_result(hardware):
    s, calls = hardware
    request = candidate(s)
    row = fleet.process(s, request)
    row['state'] = 'activating'
    publisher.atomic_write_json(fleet.record_path(s, request['deployment']), row)
    publisher.swap_gadget_image(s, 'A', 'B')
    fleet.reconcile(s)
    request['operation'] = 'activate'
    assert fleet.process(s, request)['state'] == 'succeeded'
    assert calls.count('swap') == 1


def test_interrupted_build_requires_retry(hardware):
    s, calls = hardware
    request = candidate(s)
    publisher.atomic_write_json(fleet.record_path(s, request['deployment']), {'id': request['deployment'], 'state': 'building'})
    assert fleet.process(s, request)['state'] == 'failed'
    assert not calls


def test_local_control_and_standalone_publish(hardware):
    s, calls = hardware
    request = candidate(s)
    fleet.process(s, request)
    with pytest.raises(RuntimeError, match='Central manager'):
        publisher.publish(s)
    publisher.atomic_write_json(s.state_dir / 'fleet/control.json', {'mode': 'local'})
    request['operation'] = 'activate'
    with pytest.raises(RuntimeError, match='Local takeover'):
        fleet.process(s, request)
    publisher.publish(s)
    assert calls == ['build', 'build', 'swap']
    assert publisher.read_json(s.status_file)['active_deployment'] is None


def test_activation_failure_not_retried(hardware, monkeypatch):
    s, calls = hardware
    request = candidate(s)
    fleet.process(s, request)
    def fail(*args):
        calls.append('failed swap')
        raise RuntimeError('USB unavailable')
    monkeypatch.setattr(publisher, 'swap_gadget_image', fail)
    request['operation'] = 'activate'
    assert fleet.process(s, request)['state'] == 'failed'
    assert fleet.process(s, request)['state'] == 'failed'
    assert calls.count('failed swap') == 1


def test_real_disk_preflight_keeps_active(hardware, monkeypatch):
    s, calls = hardware
    from collections import namedtuple
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(fleet.shutil, 'disk_usage', lambda _: usage(100, 100, 0))
    result = fleet.process(s, candidate(s))
    assert result['state'] == 'failed' and 'Insufficient' in result['error']
    assert not calls and s.image_a.read_bytes() == b'old image'


def test_superseded_prepared_image_cannot_activate(hardware):
    s, calls = hardware
    old, new = candidate(s), candidate(s)
    fleet.process(s, old)
    fleet.process(s, new)
    old['operation'] = 'activate'
    with pytest.raises(RuntimeError, match='superseded'):
        fleet.process(s, old)
    assert calls == ['build', 'build']


def test_preparation_serializes_on_publisher_lock(hardware):
    s, calls = hardware
    import concurrent.futures
    import threading
    started = threading.Event()
    request = candidate(s)
    def prepare():
        started.set()
        return fleet.process(s, request)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        with publisher.exclusive_lock(s.manager_lock):
            future = pool.submit(prepare)
            assert started.wait(2)
            with pytest.raises(concurrent.futures.TimeoutError):
                future.result(timeout=.1)
            assert not calls
        assert future.result(timeout=5)['state'] == 'prepared'
