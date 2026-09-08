"""Read-only Pi snapshot import and immutable content transfer."""
import hashlib
import os
import time
from pathlib import Path
from flask import abort, g, jsonify, request, render_template, send_from_directory
from sqlalchemy import select
from manager.models import Device, PiSnapshot, Deployment
from protocol import validate_manifest


def pending(db, device):
    return db.scalar(select(PiSnapshot).where(PiSnapshot.device_id == device,
        PiSnapshot.state.in_(['requested', 'uploading'])).order_by(PiSnapshot.created, PiSnapshot.id).limit(1))


def register(app, blobs, locked, audit, check_blobs):
    def owned(key):
        row = locked(PiSnapshot, key)
        if row.device_id != g.device.id:
            abort(403)
        return row

    @app.get('/preview-assets/<path:filename>')
    def preview_asset(filename):
        return send_from_directory(Path(__file__).resolve().parents[1] / 'opt/piusb/web/gcode_viewer/static', filename)

    @app.get('/preview/<digest>/<name>')
    def preview(digest, name):
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            abort(404)
        if Path(name).suffix.lower() not in ('.gcode', '.gco', '.g', '.nc'):
            abort(415)
        path = blobs / digest
        if not path.is_file():
            abort(404)
        if path.stat().st_size > 100 * 1024**2:
            abort(422, 'Preview is limited to 100 MiB')
        return render_template('preview.html', relative=name, digest=digest,
                               size=path.stat().st_size, max_bytes=100 * 1024**2)

    @app.post('/api/v1/devices/<key>/snapshots/<snapshot>/cancel')
    def cancel_snapshot(key, snapshot):
        locked(Device, key)
        row = locked(PiSnapshot, snapshot)
        if row.device_id != key or row.state not in ('requested', 'uploading'):
            abort(409)
        row.state, row.error = 'failed', 'Canceled by administrator'
        audit('pi-snapshot-canceled', {'device': key, 'snapshot': snapshot})
        g.db.commit()
        return jsonify(ok=True)

    @app.get('/api/v1/devices/<key>/snapshots')
    def list_snapshots(key):
        locked(Device, key)
        rows = g.db.scalars(select(PiSnapshot).where(PiSnapshot.device_id == key).order_by(PiSnapshot.created.desc()))
        return jsonify(snapshots=[{'id': r.id, 'source': r.source, 'state': r.state,
            'created': r.created, 'manifest': r.manifest, 'error': r.error} for r in rows])

    @app.post('/api/v1/devices/<key>/snapshots')
    def capture(key):
        d = locked(Device, key)
        source = request.get_json().get('source')
        if source not in ('active', 'staging'):
            raise ValueError('Choose active or staging contents')
        if pending(g.db, key):
            abort(409, 'A snapshot is already being imported')
        if g.db.scalar(select(Deployment).where(Deployment.device_id == key,
                Deployment.state.not_in(['succeeded', 'failed', 'canceled'])).limit(1)):
            abort(409, 'Finish or cancel pending deployments before importing Pi contents')
        row = PiSnapshot(device_id=d.id, source=source)
        g.db.add(row)
        audit('pi-snapshot-requested', {'device': key, 'source': source})
        g.db.commit()
        return jsonify(id=row.id), 201

    @app.post('/api/v1/devices/<key>/snapshots/<snapshot>/draft')
    def import_draft(key, snapshot):
        d, row = locked(Device, key), locked(PiSnapshot, snapshot)
        if row.device_id != key or row.state != 'ready':
            abort(409, 'Snapshot is not available for this device')
        if request.get_json().get('revision') != d.revision:
            abort(409, 'Draft changed; reload before importing')
        check_blobs(row.manifest)
        d.draft, d.revision = row.manifest, d.revision + 1
        audit('pi-snapshot-copied-to-draft', {'device': key, 'snapshot': snapshot})
        g.db.commit()
        return jsonify(revision=d.revision)

    @app.post('/api/v1/device/snapshots/<key>/manifest')
    def manifest(key):
        row = owned(key)
        data = request.get_json()
        if row.state not in ('requested', 'uploading'):
            abort(409)
        if data.get('error'):
            row.state, row.error = 'failed', str(data['error'])[:8000]
            audit('pi-snapshot-failed', {'snapshot': key, 'error': row.error})
            g.db.commit()
            return jsonify(ok=True)
        value = data['manifest']
        validate_manifest(value)
        if row.state == 'uploading' and row.manifest != value:
            abort(409, 'Snapshot manifest is immutable')
        row.manifest, row.state = value, 'uploading'
        missing = []
        for item in value:
            if item['kind'] != 'file':
                continue
            digest = item['sha256']
            target = blobs / digest
            if target.is_file() and target.stat().st_size == item['size']:
                continue
            part = blobs / ('.pi-' + key + '-' + digest)
            missing.append({'sha256': digest, 'size': item['size'], 'offset': part.stat().st_size if part.exists() else 0})
        g.db.commit()
        return jsonify(missing=missing)

    @app.put('/api/v1/device/snapshots/<key>/content/<digest>')
    def content(key, digest):
        row = owned(key)
        item = next((x for x in row.manifest if x.get('sha256') == digest), None)
        if row.state != 'uploading' or item is None:
            abort(403)
        offset = int(request.args.get('offset', '-1'))
        part = blobs / ('.pi-' + key + '-' + digest)
        size = part.stat().st_size if part.exists() else 0
        if offset != size:
            abort(409, 'Upload offset changed; request manifest to resume')
        chunk = request.stream.read(8 * 1024**2 + 1)
        if len(chunk) > 8 * 1024**2 or size + len(chunk) > item['size']:
            abort(413)
        with part.open('ab') as stream:
            stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        size += len(chunk)
        if size == item['size']:
            with part.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != digest:
                part.unlink()
                raise ValueError('Snapshot content hash mismatch; upload must restart')
            os.replace(part, blobs / digest)
        g.db.commit()
        return jsonify(offset=size)

    @app.post('/api/v1/device/snapshots/<key>/complete')
    def complete(key):
        row = owned(key)
        if row.state == 'ready':
            return jsonify(ok=True)
        if row.state != 'uploading':
            abort(409)
        check_blobs(row.manifest)
        row.state = 'ready'
        audit('pi-snapshot-ready', {'snapshot': key, 'device': row.device_id})
        # Keep only the newest successful import per source. Drafts and deployments
        # retain their own immutable manifests and content references.
        for old in g.db.scalars(select(PiSnapshot).where(PiSnapshot.device_id == row.device_id,
                PiSnapshot.source == row.source, PiSnapshot.state == 'ready', PiSnapshot.id != key)):
            g.db.delete(old)
        g.db.commit()
        return jsonify(ok=True)
