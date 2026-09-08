import json
import uuid
from tests.test_fleet import hardware, publisher
from pi_export import export
import fleet


def test_staging_export_preserves_contents(hardware):
    s, calls = hardware
    (s.staging_dir / 'empty').mkdir()
    (s.staging_dir / 'x.txt').write_text('original')
    key = str(uuid.uuid4())
    result = fleet.process(s, {'operation': 'export', 'request_id': key, 'source': 'staging'})
    assert result['state'] == 'ready'
    assert result['manifest'][0] == {'path': 'empty', 'kind': 'dir'}
    item = result['manifest'][1]
    assert (s.state_dir / 'pi-export' / item['sha256']).read_text() == 'original'
    assert (s.staging_dir / 'x.txt').read_text() == 'original'
    assert s.image_a.read_bytes() == b'old image' and calls == []
    (s.staging_dir / 'x.txt').write_text('new')
    assert fleet.process(s, {'operation': 'export', 'request_id': key, 'source': 'staging'})['manifest'] == result['manifest']


def test_export_symlink_and_wrong_backing_fail(hardware):
    s, calls = hardware
    (s.staging_dir / 'escape').symlink_to('/etc/passwd')
    assert export(s, str(uuid.uuid4()), 'staging')['state'] == 'failed'
    (s.lun_path / 'file').write_text('/unexpected/image')
    assert export(s, str(uuid.uuid4()), 'active')['state'] == 'failed'
    assert not calls


def test_active_export_mounts_read_only_and_unmounts(hardware, monkeypatch):
    s, calls = hardware
    (s.mount_dir / 'visible.gcode').write_text('G1 X1')
    commands = []
    monkeypatch.setattr(publisher, 'is_mountpoint', lambda p: False)
    monkeypatch.setattr(publisher, 'run', lambda args: commands.append(args))
    result = export(s, str(uuid.uuid4()), 'active')
    assert result['state'] == 'ready'
    assert 'loop,ro,nodev,nosuid,noexec' in commands[0]
    assert commands[-1][0].endswith('umount')
    assert s.image_a.read_bytes() == b'old image' and calls == []


def test_export_preflight_space(hardware, monkeypatch):
    import pi_export
    from types import SimpleNamespace
    s, calls = hardware
    (s.staging_dir / 'x').write_text('data')
    monkeypatch.setattr(pi_export.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0))
    result = export(s, str(uuid.uuid4()), 'staging')
    assert result['state'] == 'failed' and 'Insufficient space' in result['error']
    assert not calls
