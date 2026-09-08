"""Root-only, fixed-operation bridge for the unprivileged fleet agent.

No command, image path, or source root is accepted from the request. Candidate
files are opened beneath a fixed directory without following symlinks and copied
into root-owned snapshots before validation/building.
"""
import dataclasses
import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from protocol import digest_file, manifest_hash, validate_manifest


def read_beneath(root, relative):
    parts = relative.split('/')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result)
            raise ValueError('Only regular candidate files are allowed')
        return os.fdopen(result, 'rb')
    finally:
        os.close(fd)


def paths(s):
    root = s.state_dir / 'fleet'
    root.mkdir(mode=0o755, exist_ok=True)
    return root, root / 'control.json'


def control(s):
    import piusb_publisher as m
    return m.read_json(s.state_dir / 'fleet' / 'control.json')


def is_managed(s):
    return control(s).get('mode') == 'managed'


def record_path(s, job):
    return s.state_dir / 'fleet' / (str(uuid.UUID(job)) + '.json')


def bound(s, slot):
    import piusb_publisher as m
    return (bool(m.read_attr(s.gadget_path / 'UDC')) and
            m.read_attr(s.lun_path / 'ro') == '1' and
            m.read_attr(s.lun_path / 'file') == str(s.image_for_slot(slot).resolve()))


def reconcile(s):
    import piusb_publisher as m
    root, _ = paths(s)
    for path in root.glob('*.json'):
        row = m.read_json(path)
        if row.get('state') == 'activating':
            if m.read_active_slot(s) == row['target'] and bound(s, row['target']):
                row['state'] = 'succeeded'
                m.update_status(s, active_deployment=row['id'], prepared_deployment=None)
            else:
                row.update(state='failed', error='Activation interrupted; inspect USB state before explicit retry')
            m.atomic_write_json(path, row)
        elif row.get('state') == 'building':
            row.update(state='failed', error='Build interrupted; explicit retry required')
            m.atomic_write_json(path, row)


def process(s, request):
    import piusb_publisher as m
    m.require_root()
    m.ensure_directories(s)
    request_id = str(uuid.UUID(request['request_id']))
    operation = request.get('operation')
    if operation not in ('prepare', 'activate', 'takeover'):
        raise ValueError('Unsupported fleet operation')
    root, control_file = paths(s)
    with m.exclusive_lock(s.manager_lock), m.exclusive_lock(s.staging_lock):
        reconcile(s)
        if operation == 'takeover':
            current = m.read_json(s.status_file).get('active_deployment')
            if current:
                source = root / 'snapshots' / str(uuid.UUID(current))
                if not source.is_dir():
                    raise RuntimeError('Active snapshot unavailable; cannot safely populate local staging')
                # Hold the staging lock throughout this copy. Local writes remain blocked by mode.
                import pwd
                user = pwd.getpwnam('piusb')
                m.run([m.command_path('rsync'), '-rlt', '--delete', '--chown=piusb:piusb',
                       str(source) + '/', str(s.staging_dir) + '/'])
                os.chown(s.staging_dir, user.pw_uid, user.pw_gid)
            m.atomic_write_json(control_file, {'mode': 'local'})
            m.update_status(s, prepared_deployment=None)
            return {'request_id': request_id, 'state': 'local'}
        job = str(uuid.UUID(request['deployment']))
        path = record_path(s, job)
        row = m.read_json(path)
        if operation == 'prepare':
            if row:
                return dict(row, request_id=request_id)
            if control(s).get('mode') == 'local' and not request.get('resume_local'):
                raise RuntimeError('Local takeover is active')
            m.atomic_write_json(control_file, {'mode': 'managed'})
            candidate = s.staging_dir.parent / 'agent' / 'candidates' / job
            with read_beneath(s.staging_dir.parent, f'agent/candidates/{job}/manifest.json') as stream:
                encoded = stream.read(16 * 1024**2 + 1)
                if len(encoded) > 16 * 1024**2:
                    raise ValueError('Manifest too large')
                manifest = json.loads(encoded)
            total = validate_manifest(manifest, s.image_size_mib * 1024**2 * s.staging_fill_percent // 100)
            if manifest_hash(manifest) != request.get('hash'):
                raise ValueError('Manifest identity mismatch')
            active = m.read_active_slot(s)
            row = {'id': job, 'state': 'building', 'old': active, 'target': 'B' if active == 'A' else 'A',
                   'hash': request['hash'], 'request_id': request_id}
            m.atomic_write_json(path, row)
            m.update_status(s, state='building', prepared_deployment=None, error=None)
            active_job = m.read_json(s.status_file).get('active_deployment')
            snapshots = root / 'snapshots'
            if snapshots.exists():
                for previous in snapshots.iterdir():
                    if previous.name != active_job and previous.is_dir() and not previous.is_symlink():
                        shutil.rmtree(previous)
            snapshot = root / 'snapshots' / job
            try:
                # Reserve conservative space for root snapshot plus the new populated image.
                if shutil.disk_usage(root).free < total + s.image_size_mib * 1024**2 + 64 * 1024**2:
                    raise RuntimeError('Insufficient free space for verified snapshot and image build')
                snapshot.mkdir(parents=True, mode=0o700)
                for item in sorted(manifest, key=lambda x: (x['path'].count('/'), x['path'])):
                    target = snapshot / item['path']
                    if item['kind'] == 'dir':
                        target.mkdir(mode=0o700)
                        continue
                    with read_beneath(s.staging_dir.parent, f"agent/candidates/{job}/files/{item['path']}") as source, target.open('xb') as dest:
                        remaining = item['size']
                        while remaining:
                            chunk = source.read(min(1024**2, remaining))
                            if not chunk:
                                raise ValueError('Candidate truncated')
                            dest.write(chunk)
                            remaining -= len(chunk)
                        if source.read(1):
                            raise ValueError('Candidate size changed')
                        dest.flush()
                        os.fsync(dest.fileno())
                    if digest_file(target) != item['sha256']:
                        raise ValueError('Candidate content hash mismatch')
                settings = dataclasses.replace(s, staging_dir=snapshot)
                stats = m.build_image(settings, s.image_for_slot(row['target']))
                row.update(state='prepared', stats=stats)
                m.update_status(s, state='idle', prepared_deployment=job, candidate_build=stats)
            except Exception as error:
                row.update(state='failed', error=str(error))
                m.update_status(s, state='error', error=str(error))
                if snapshot.exists():
                    shutil.rmtree(snapshot)
            m.atomic_write_json(path, row)
            return row
        if not row:
            raise RuntimeError('No prepared image for this deployment')
        if row['state'] in ('succeeded', 'failed'):
            return dict(row, request_id=request_id)
        if not is_managed(s):
            raise RuntimeError('Local takeover blocks activation')
        status = m.read_json(s.status_file)
        if (row['state'] != 'prepared' or status.get('prepared_deployment') != job or
                m.read_active_slot(s) != row['old']):
            raise RuntimeError('Prepared image was superseded; explicit retry required')
        row.update(state='activating', request_id=request_id)
        m.atomic_write_json(path, row)
        m.update_status(s, state='switching')
        try:
            m.swap_gadget_image(s, row['old'], row['target'])
            if not bound(s, row['target']):
                raise RuntimeError('Expected read-only USB backing image is not bound')
            row['state'] = 'succeeded'
            m.update_status(s, state='idle', active_deployment=job, prepared_deployment=None,
                            active=row['target'], active_build=row['stats'], gadget_online=True, error=None)
        except Exception as error:
            row.update(state='failed', error=str(error))
            m.update_status(s, state='error', error=str(error))
        m.atomic_write_json(path, row)
        # Retain only the active and prepared source snapshots; never remove image files here.
        status = m.read_json(s.status_file)
        retain = {status.get('active_deployment'), status.get('prepared_deployment')}
        snapshots = root / 'snapshots'
        if snapshots.exists():
            for child in snapshots.iterdir():
                if child.name not in retain and child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
        return row


def consume(s):
    import piusb_publisher as m
    request_file = s.runtime_dir / ('takeover.request' if (s.runtime_dir / 'takeover.request').exists() else 'fleet.request')
    try:
        with read_beneath(s.runtime_dir, request_file.name) as stream:
            data = stream.read(65537)
        request_file.unlink(missing_ok=True)
        if len(data) > 65536:
            raise ValueError('Fleet request too large')
        request = json.loads(data)
        result = process(s, request)
    except Exception as error:
        result = {'state': 'failed', 'error': str(error),
                  'request_id': locals().get('request', {}).get('request_id')}
    m.atomic_write_json(s.state_dir / 'fleet-result.json', result)
