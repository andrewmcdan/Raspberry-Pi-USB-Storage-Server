"""Privileged read-only snapshot of the active USB filesystem or local staging."""
import hashlib
import os
import shutil
import stat
from pathlib import Path
from protocol import validate_manifest


def export(s, request_id, source):
    import piusb_publisher as m
    if source not in ('active', 'staging'):
        raise ValueError('Invalid snapshot source')
    root = s.state_dir / 'pi-export'
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o755)
    result = {'id': request_id, 'source': source, 'state': 'failed'}
    mounted = False
    try:
        origin = s.staging_dir
        if source == 'active':
            if not m.read_attr(s.gadget_path / 'UDC') or m.read_attr(s.lun_path / 'ro') != '1':
                raise RuntimeError('Active USB drive must be bound read-only before capture')
            image = s.image_for_slot(m.read_active_slot(s)).resolve()
            if m.read_attr(s.lun_path / 'file') != str(image):
                raise RuntimeError('Active image does not match the USB backing image')
            if m.is_mountpoint(s.mount_dir):
                raise RuntimeError('Build mount is busy; finish recovery before snapshot')
            m.run([m.command_path('mount'), '-o', 'loop,ro,nodev,nosuid,noexec', str(image), str(s.mount_dir)])
            mounted, origin = True, s.mount_dir
        entries, total = [], 0
        for base, dirs, files in os.walk(origin, followlinks=False):
            for name in sorted(dirs + files):
                path = Path(base) / name
                info = path.lstat()
                relative = path.relative_to(origin).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    entries.append({'path': relative, 'kind': 'dir'})
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    entries.append({'path': relative, 'kind': 'file', 'size': info.st_size, 'sha256': '0' * 64})
                else:
                    raise ValueError('Snapshot contains a symlink or special file: ' + relative)
        validate_manifest(entries)
        if shutil.disk_usage(root).free < total + 64 * 1024**2:
            raise RuntimeError('Insufficient space for a read-only content snapshot')
        from fleet import read_beneath
        for index, item in enumerate(entries):
            if item['kind'] == 'dir':
                continue
            temporary = root / ('part-' + str(index))
            digest, copied = hashlib.sha256(), 0
            with read_beneath(origin, item['path']) as src, temporary.open('xb') as dst:
                while chunk := src.read(1024**2):
                    copied += len(chunk)
                    if copied > item['size']:
                        raise ValueError('Source changed during capture')
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if copied != item['size']:
                raise ValueError('Source changed during capture')
            item['sha256'] = digest.hexdigest()
            os.chmod(temporary, 0o644)
            os.replace(temporary, root / item['sha256'])
        result.update(state='ready', manifest=sorted(entries, key=lambda x: x['path']))
    except Exception as error:
        result['error'] = str(error)
    finally:
        if mounted:
            try:
                m.run([m.command_path('umount'), str(s.mount_dir)])
            except Exception as error:
                result.update(state='failed', error='Snapshot unmount failed: ' + str(error))
        m.atomic_write_json(s.state_dir / 'pi-export.json', result)
    return dict(result, request_id=request_id)
