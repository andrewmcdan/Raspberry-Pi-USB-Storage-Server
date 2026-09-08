"""Authenticated manager UI and versioned pull protocol."""
import copy
import hashlib
import hmac
import os
import secrets
import sys
import time
import uuid
import shlex
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError
from sqlalchemy.exc import IntegrityError

from flask import Flask, abort, g, jsonify, render_template, request, send_file, session
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash

sys.path.append(str(Path(__file__).resolve().parents[1] / 'opt' / 'piusb'))
from protocol import MAX_FILE, diff, manifest_hash, valid_path, validate_manifest
from manager.models import Audit, Base, Collection, Deployment, Device, Enrollment, Group, Review, uid

TERMINAL = {'succeeded', 'failed', 'canceled'}
STATES = {'queued', 'downloading', 'building', 'prepared', 'switching'} | TERMINAL


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def in_window(window, now):
    if not window:
        return True
    local = datetime.fromtimestamp(now, ZoneInfo(window['timezone']))
    minute = local.hour * 60 + local.minute
    start, end = window['start'], window['end']
    if start < end:
        return local.weekday() in window['days'] and start <= minute < end
    return ((local.weekday() in window['days'] and minute >= start) or
            ((local.weekday() - 1) % 7 in window['days'] and minute < end))


def validate_window(window):
    if not window:
        return {}
    ZoneInfo(window['timezone'])
    if (not isinstance(window.get('days'), list) or not window['days'] or
            any(type(x) is not int or x not in range(7) for x in window['days']) or
            any(type(window.get(x)) is not int or not 0 <= window[x] < 1440 for x in ('start', 'end')) or
            window['start'] == window['end']):
        raise ValueError('Window needs weekdays 0–6 and distinct start/end minutes 0–1439')
    return window


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY'),
                      DATABASE_URL=os.environ.get('DATABASE_URL', 'sqlite:///manager.db'),
                      CONTENT_DIR=os.environ.get('CONTENT_DIR', '/data/content'),
                      ADMIN_PASSWORD_HASH=os.environ.get('ADMIN_PASSWORD_HASH', ''),
                      PUBLIC_URL=os.environ.get('PUBLIC_URL', 'https://piusb.example.internal'),
                      SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
                      SESSION_COOKIE_SAMESITE='Strict', MAX_CONTENT_LENGTH=MAX_FILE + 1024**2)
    if config:
        app.config.update(config)
    if not app.secret_key or not app.config['ADMIN_PASSWORD_HASH']:
        raise RuntimeError('Set SECRET_KEY and ADMIN_PASSWORD_HASH before starting')
    engine = create_engine(app.config['DATABASE_URL'], pool_pre_ping=True)
    app.extensions['db'] = sessionmaker(engine, expire_on_commit=False)
    app.extensions['engine'] = engine
    blobs = Path(app.config['CONTENT_DIR'])
    blobs.mkdir(parents=True, exist_ok=True)

    def audit(action, detail):
        g.db.add(Audit(actor=getattr(g, 'device', None).id if getattr(g, 'device', None) else 'admin',
                       action=action, detail=detail))

    def locked(model, key):
        value = g.db.scalar(select(model).where(model.id == key).with_for_update())
        if value is None:
            abort(404)
        return value

    def file_set(kind, key):
        if kind not in ('devices', 'collections'):
            abort(404)
        return locked(Device if kind == 'devices' else Collection, key)

    def check_blobs(manifest):
        validate_manifest(manifest)
        for item in manifest:
            if item['kind'] == 'file':
                path = blobs / item['sha256']
                if not path.is_file() or path.stat().st_size != item['size']:
                    raise ValueError(f"Missing content for {item['path']}")

    @app.before_request
    def authorize():
        g.db = app.extensions['db']()
        if request.path != '/api/v1/content' and request.content_length and request.content_length > 16 * 1024**2:
            abort(413, 'JSON request exceeds 16 MiB')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and g.db.bind.dialect.name == 'postgresql':
            # Common ordering with retention; streamed blob uploads are protected by the grace period.
            if request.path != '/api/v1/content':
                g.db.execute(text('SELECT pg_advisory_xact_lock(731952)'))
        if request.path in ('/health', '/login'):
            return
        if request.path.startswith('/api/v1/device/'):
            auth = request.headers.get('Authorization', '')
            if not auth.startswith('Bearer '):
                abort(401)
            # Lock before reading state so simultaneous check-ins cannot issue conflicting work.
            device = g.db.scalar(select(Device).where(Device.credential == token_hash(auth[7:])).with_for_update())
            if device is None or device.revoked:
                abort(401)
            g.device = device
            return
        if request.path == '/api/v1/enroll':
            return
        if not session.get('admin'):
            if request.path == '/':
                return render_template('login.html')
            abort(401)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(supplied, session.get('csrf', 'missing')):
                abort(400, 'Invalid CSRF token')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.teardown_request
    def close(_error):
        if hasattr(g, 'db'):
            g.db.rollback()
            g.db.close()

    @app.errorhandler(ValueError)
    @app.errorhandler(ZoneInfoNotFoundError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(KeyError)
    @app.errorhandler(TypeError)
    def invalid_shape(error):
        return jsonify(error='Missing or invalid request field: ' + str(error)), 400

    @app.errorhandler(IntegrityError)
    def conflict(_error):
        return jsonify(error='This name or hardware identity is already in use'), 409

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.get('/health')
    def health():
        g.db.execute(select(1))
        return jsonify(ok=True)

    @app.post('/login')
    def login():
        # Prevent cross-origin login submissions as well as session fixation.
        origin = request.headers.get('Origin')
        if origin and origin.rstrip('/') != app.config['PUBLIC_URL'].rstrip('/'):
            abort(403)
        if not check_password_hash(app.config['ADMIN_PASSWORD_HASH'], request.form.get('password', '')):
            return render_template('login.html', error='Incorrect password'), 401
        session.clear()
        session.update(admin=True, csrf=secrets.token_urlsafe(32))
        session.permanent = True
        return '', 303, {'Location': '/'}

    @app.post('/api/v1/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/')
    def index():
        return render_template('index.html', csrf=session['csrf'])

    @app.get('/api/v1/inventory')
    def inventory():
        devices = []
        for d in g.db.scalars(select(Device).order_by(Device.name)):
            devices.append({k: getattr(d, k) for k in ('id', 'serial', 'name', 'location', 'tags', 'notes',
                            'window', 'last_seen', 'telemetry', 'paused', 'local', 'revoked', 'revision')} |
                           {'online': time.time() - d.last_seen < 60})
        return jsonify(devices=devices,
                       groups=[{'id': x.id, 'name': x.name, 'devices': x.devices} for x in g.db.scalars(select(Group))],
                       collections=[{'id': x.id, 'name': x.name} for x in g.db.scalars(select(Collection))])

    @app.post('/api/v1/enrollment-tokens')
    def enrollment_token():
        token = secrets.token_urlsafe(32)
        g.db.add(Enrollment(token=token_hash(token), expires=time.time() + 900))
        audit('enrollment-token-created', {})
        g.db.commit()
        return jsonify(token=token, expires_in=900,
                       command=f"sudo python3 /opt/piusb/agent.py enroll --url {shlex.quote(app.config['PUBLIC_URL'])} --token {token}"), 201

    @app.post('/api/v1/enroll')
    def enroll():
        data = request.get_json()
        row = g.db.scalar(select(Enrollment).where(Enrollment.token == token_hash(data.get('token', ''))).with_for_update())
        if row is None or row.used or row.expires < time.time():
            abort(401, 'Enrollment token is expired or already used')
        device_id = str(uuid.UUID(data['id']))
        serial = str(data['serial']).strip()
        if not serial or len(serial) > 200:
            raise ValueError('Hardware serial is required')
        existing = g.db.get(Device, device_id)
        serial_owner = g.db.scalar(select(Device).where(Device.serial == serial))
        if (existing and existing.serial != serial) or (serial_owner and serial_owner.id != device_id):
            abort(409, 'Hardware identity conflicts with an enrolled device; revoke and investigate before reenrollment')
        if existing and not existing.revoked:
            abort(409, 'Device is already enrolled; revoke its old credential before reenrollment')
        credential = secrets.token_urlsafe(48)
        if existing:
            existing.credential, existing.revoked = token_hash(credential), False
        else:
            g.db.add(Device(id=device_id, serial=serial, credential=token_hash(credential),
                            name=str(data.get('hostname', device_id))[:200]))
        row.used = True
        audit('enrolled', {'device': device_id, 'serial': serial})
        g.db.commit()
        return jsonify(id=device_id, credential=credential), 201

    @app.patch('/api/v1/devices/<key>')
    def edit_device(key):
        d, data = locked(Device, key), request.get_json()
        for field in ('name', 'location', 'notes'):
            if field in data:
                value = str(data[field]).strip()
                if len(value) > (10000 if field == 'notes' else 200) or (field == 'name' and not value):
                    raise ValueError('Invalid device text')
                setattr(d, field, value)
        if 'tags' in data:
            if not isinstance(data['tags'], list) or any(not isinstance(x, str) or len(x) > 100 for x in data['tags']):
                raise ValueError('Tags must be short strings')
            d.tags = data['tags']
        if 'window' in data:
            d.window = validate_window(data['window'])
        if 'paused' in data:
            d.paused = bool(data['paused'])
        if data.get('revoke'):
            d.revoked = True
        audit('device-updated', {'device': key, 'changes': data})
        g.db.commit()
        return jsonify(ok=True)

    @app.post('/api/v1/groups')
    def save_group():
        data = request.get_json()
        members = sorted(set(data.get('devices', [])))
        for key in members:
            if g.db.get(Device, key) is None:
                raise ValueError('Unknown group member')
        name = str(data['name']).strip()
        if not name or len(name) > 200:
            raise ValueError('Group name is required (maximum 200 characters)')
        row = locked(Group, data['id']) if data.get('id') else Group()
        row.name, row.devices = name, members
        g.db.add(row)
        audit('group-saved', {'name': name, 'devices': members})
        g.db.commit()
        return jsonify(id=row.id)

    @app.post('/api/v1/collections')
    def add_collection():
        name = str(request.get_json()['name']).strip()
        if not name or len(name) > 200:
            raise ValueError('Collection name is required')
        row = Collection(name=name)
        g.db.add(row)
        audit('collection-created', {'name': name})
        g.db.commit()
        return jsonify(id=row.id), 201

    @app.get('/api/v1/sets/<kind>/<key>')
    def get_set(kind, key):
        row = file_set(kind, key)
        return jsonify(manifest=row.draft, revision=row.revision)

    @app.put('/api/v1/sets/<kind>/<key>')
    def put_set(kind, key):
        row, data = file_set(kind, key), request.get_json()
        if data.get('revision') != row.revision:
            abort(409, 'Draft changed in another window; reload before editing')
        check_blobs(data['manifest'])
        row.draft, row.revision = data['manifest'], row.revision + 1
        audit('draft-saved', {'kind': kind, 'id': key, 'revision': row.revision})
        g.db.commit()
        return jsonify(revision=row.revision)

    @app.post('/api/v1/content')
    def upload():
        # Raw streamed body: no multipart spool or multi-GB in-memory buffering.
        temp = blobs / ('.upload-' + uid())
        digest, size = hashlib.sha256(), 0
        try:
            with temp.open('xb') as stream:
                while chunk := request.stream.read(1024**2):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise ValueError('File exceeds FAT32 maximum')
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            name = digest.hexdigest()
            os.replace(temp, blobs / name)
            return jsonify(sha256=name, size=size), 201
        finally:
            temp.unlink(missing_ok=True)

    @app.get('/api/v1/content/<digest>')
    def admin_download(digest):
        if len(digest) != 64 or any(x not in '0123456789abcdef' for x in digest):
            abort(404)
        path = blobs / digest
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=digest, conditional=True, etag=digest)

    @app.post('/api/v1/reviews')
    def review():
        data = request.get_json()
        targets = set(data.get('devices', []))
        for key in data.get('groups', []):
            targets.update(locked(Group, key).devices)
        if not targets:
            raise ValueError('Select at least one Pi')
        policy = data.get('policy', 'auto')
        if policy not in ('auto', 'hold', 'scheduled', 'window'):
            raise ValueError('Unknown activation policy')
        due = float(data.get('due') or 0)
        if not 0 <= due < 32503680000 or (policy == 'scheduled' and not due):
            raise ValueError('Select a valid scheduled date')
        source = None
        if data.get('collection'):
            source = locked(Collection, data['collection']).draft
        if data.get('version'):
            source = locked(Deployment, data['version']).manifest
            if source is None:
                raise ValueError('This historical content has expired')
        entries = []
        for key in sorted(targets):
            d = locked(Device, key)
            if d.revoked:
                raise ValueError(f'{d.name} has a revoked credential')
            manifest = copy.deepcopy(source if source is not None else d.draft)
            check_blobs(manifest)
            capacity = d.telemetry.get('capacity')
            if type(capacity) is not int or capacity <= 0:
                raise ValueError(f'{d.name} must check in with its capacity before deployment')
            total = validate_manifest(manifest, capacity)
            if policy == 'window' and not d.window:
                raise ValueError(f'{d.name} needs a maintenance window')
            active_id = d.telemetry.get('active_deployment')
            active = g.db.get(Deployment, active_id) if active_id else None
            old = active.manifest if active and active.manifest is not None else []
            entries.append({'device': key, 'name': d.name, 'serial': d.serial, 'manifest': manifest,
                            'hash': manifest_hash(manifest), 'total': total, 'diff': diff(old, manifest),
                            'local_takeover': d.local, 'current_contents_known': bool(active) and not d.local})
        row = Review(payload={'entries': entries, 'policy': policy, 'due': due,
                              'resume_local': bool(data.get('resume_local'))})
        if any(x['local_takeover'] for x in entries) and not row.payload['resume_local']:
            raise ValueError('Local takeover is active. Select return to manager and review the full replacement.')
        g.db.add(row)
        g.db.commit()
        return jsonify(id=row.id, **row.payload), 201

    @app.post('/api/v1/reviews/<key>/deploy')
    def deploy(key):
        row = locked(Review, key)
        if row.submitted:
            return jsonify(batch=key)
        if time.time() - row.created > 900:
            abort(409, 'Review expired; review the deployment again')
        for entry in row.payload['entries']:
            d = locked(Device, entry['device'])
            if d.revoked or (d.local and not row.payload['resume_local']):
                abort(409, 'Device control changed; review again')
            validate_manifest(entry['manifest'], d.telemetry.get('capacity', 0))
            check_blobs(entry['manifest'])
            g.db.add(Deployment(batch=key, device_id=d.id, manifest=entry['manifest'],
                                manifest_hash=entry['hash'], policy=row.payload['policy'],
                                due=row.payload['due'], resume_local=row.payload['resume_local']))
        row.submitted = True
        audit('deployment-submitted', {'batch': key, 'devices': [e['device'] for e in row.payload['entries']]})
        g.db.commit()
        return jsonify(batch=key), 201

    @app.get('/api/v1/deployments')
    def deployments():
        rows = g.db.scalars(select(Deployment).order_by(Deployment.created.desc()).limit(500))
        return jsonify(deployments=[{k: getattr(x, k) for k in ('id', 'batch', 'device_id', 'created', 'state',
                        'policy', 'due', 'approved', 'canceled', 'pinned', 'progress', 'error', 'manifest_hash')} |
                        {'retained': x.manifest is not None} for x in rows])

    @app.get('/api/v1/deployments/<key>/manifest')
    def deployment_manifest(key):
        row = locked(Deployment, key)
        return jsonify(manifest=row.manifest)

    @app.post('/api/v1/deployments/<key>/<action>')
    def deployment_action(key, action):
        # Consistent lock ordering: device, then deployment.
        initial = g.db.get(Deployment, key)
        if initial is None:
            abort(404)
        locked(Device, initial.device_id)
        row = locked(Deployment, key)
        if action == 'approve' and row.state not in TERMINAL:
            row.approved = True
        elif action == 'cancel' and row.state not in TERMINAL:
            row.canceled = True
            if row.state == 'queued':
                row.state = 'canceled'
        elif action == 'pin':
            if row.manifest is None:
                abort(409, 'Content has already expired')
            row.pinned = not row.pinned
        elif action == 'retry' and row.state in ('failed', 'canceled'):
            if row.manifest is None:
                abort(409, 'Content has expired')
            new = Deployment(batch=uid(), device_id=row.device_id, manifest=copy.deepcopy(row.manifest),
                             manifest_hash=row.manifest_hash, policy=row.policy, due=row.due)
            g.db.add(new)
        else:
            abort(409, 'Action is not valid in this state')
        audit('deployment-' + action, {'deployment': key})
        g.db.commit()
        return jsonify(ok=True)

    @app.get('/api/v1/audit')
    def audit_log():
        return jsonify(events=[{'at': x.created, 'actor': x.actor, 'action': x.action, 'detail': x.detail}
                       for x in g.db.scalars(select(Audit).order_by(Audit.id.desc()).limit(500))])

    @app.post('/api/v1/device/check-in')
    def check_in():
        d, data = g.device, request.get_json()
        if data.get('serial') != d.serial or data.get('id') != d.id:
            abort(409, 'Hardware identity mismatch')
        telemetry = data.get('telemetry', {})
        if not isinstance(telemetry, dict) or len(str(telemetry)) > 32000:
            raise ValueError('Invalid telemetry')
        d.last_seen, d.telemetry = time.time(), telemetry
        d.local = bool(telemetry.get('local', False))
        report = data.get('report')
        if report:
            row = locked(Deployment, report['id'])
            if row.device_id != d.id:
                abort(403)
            state = report.get('state')
            if state not in STATES - {'queued'}:
                raise ValueError('Invalid reported state')
            if row.state not in TERMINAL:
                allowed = {'downloading': {'downloading', 'building', 'prepared', 'failed', 'canceled'},
                           'building': {'building', 'prepared', 'failed', 'canceled'},
                           'prepared': {'prepared', 'switching', 'failed', 'canceled'},
                           'switching': {'switching', 'succeeded', 'failed'}}
                if row.state == 'switching' and state == 'prepared':
                    # Authorization response may have been lost. Reissue the same authorization.
                    state = 'switching'
                if state not in allowed.get(row.state, set()):
                    abort(409, 'Invalid deployment state transition')
                if state == 'succeeded' and telemetry.get('active_deployment') != row.id:
                    abort(409, 'Success requires the active deployment identity')
                if row.state != state:
                    audit('deployment-state', {'deployment': row.id, 'from': row.state, 'to': state,
                                               'error': str(report.get('error', ''))[:8000]})
                row.state = state
                row.progress = report.get('progress', {})
                row.error = str(report.get('error', ''))[:8000]
        row = g.db.scalar(select(Deployment).where(Deployment.device_id == d.id,
                          Deployment.state.not_in(TERMINAL)).order_by(Deployment.created, Deployment.id).limit(1))
        assignment = None
        if row:
            if row.state == 'queued' and not d.paused and (not d.local or row.resume_local):
                row.state = 'downloading'
            if row.state != 'queued':
                may_activate = (not d.paused and (not d.local or row.resume_local) and not row.canceled and
                                time.time() >= row.due and (row.policy != 'hold' or row.approved) and
                                (row.policy != 'window' or in_window(d.window, time.time())))
                # Record authorization before returning it: restart reconciliation can then report success.
                if row.state == 'prepared' and may_activate:
                    row.state = 'switching'
                    audit('activation-authorized', {'deployment': row.id})
                assignment = {'id': row.id, 'manifest': row.manifest, 'hash': row.manifest_hash,
                              'state': row.state, 'cancel': row.canceled,
                              'activate': row.state == 'switching', 'resume_local': row.resume_local}
        g.db.commit()
        return jsonify(assignment=assignment, paused=d.paused, poll_seconds=15)

    @app.get('/api/v1/device/content/<digest>')
    def device_content(digest):
        if len(digest) != 64 or any(x not in '0123456789abcdef' for x in digest):
            abort(404)
        rows = g.db.scalars(select(Deployment).where(Deployment.device_id == g.device.id,
                            Deployment.state.not_in(TERMINAL)))
        if not any(any(x.get('sha256') == digest for x in row.manifest or []) for row in rows):
            abort(403)
        path = blobs / digest
        if not path.is_file():
            abort(404)
        # Release device row lock before a slow multi-GB transfer.
        g.db.commit()
        return send_file(path, as_attachment=True, download_name=digest, conditional=True, etag=digest)

    return app


if __name__ == '__main__':
    app = create_app()
    if '--init-db' in sys.argv:
        Base.metadata.create_all(app.extensions['engine'])
    else:
        app.run()
