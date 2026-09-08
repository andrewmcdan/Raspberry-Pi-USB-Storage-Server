import hashlib
import time
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from manager.app import create_app, in_window
from manager.models import Audit, Base, Deployment, Device, Enrollment, Review
from manager.worker import collect


@pytest.fixture
def app(tmp_path):
    value = create_app({'TESTING': True, 'SECRET_KEY': 'test-secret', 'ADMIN_PASSWORD_HASH': generate_password_hash('test'),
                        'CONTENT_DIR': str(tmp_path / 'content'), 'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'db'),
                        'SESSION_COOKIE_SECURE': False})
    Base.metadata.create_all(value.extensions['engine'])
    return value


@pytest.fixture
def admin(app):
    client = app.test_client()
    with client.session_transaction() as s:
        s.update(admin=True, csrf='test')
    client.environ_base['HTTP_X_CSRF_TOKEN'] = 'test'
    return client


def enroll(app, admin, serial='serial-1'):
    token = admin.post('/api/v1/enrollment-tokens', json={}).json['token']
    client = app.test_client()
    identity = str(uuid.uuid4())
    response = client.post('/api/v1/enroll', json={'token': token, 'id': identity, 'serial': serial, 'hostname': serial})
    assert response.status_code == 201
    credential = response.json['credential']
    client.environ_base['HTTP_AUTHORIZATION'] = 'Bearer ' + credential
    payload = {'id': identity, 'serial': serial, 'telemetry': {'capacity': 1000000}}
    assert client.post('/api/v1/device/check-in', json=payload).status_code == 200
    return client, payload, token


def upload(admin, content=b'hello', path='hello.txt'):
    r = admin.post('/api/v1/content', data=content, content_type='application/octet-stream')
    assert r.status_code == 201
    return dict(r.json, path=path, kind='file')


def deploy(admin, payload, manifest, policy='auto', **extra):
    identity = payload['id']
    draft = admin.get('/api/v1/sets/devices/' + identity).json
    assert admin.put('/api/v1/sets/devices/' + identity, json={'revision': draft['revision'], 'manifest': manifest}).status_code == 200
    review = admin.post('/api/v1/reviews', json={'devices': [identity], 'policy': policy, **extra})
    assert review.status_code == 201, review.json
    assert admin.post('/api/v1/reviews/' + review.json['id'] + '/deploy', json={}).status_code == 201
    return next(d for d in admin.get('/api/v1/deployments').json['deployments'] if d['batch'] == review.json['id'])


def test_enrollment_auth_identity(app, admin):
    pi, payload, token = enroll(app, admin)
    assert app.test_client().get('/api/v1/inventory').status_code == 401
    assert pi.get('/api/v1/inventory').status_code == 401
    assert app.test_client().post('/api/v1/enroll', json={'token': token}).status_code == 401
    bad = dict(payload, serial='other')
    assert pi.post('/api/v1/device/check-in', json=bad).status_code == 409
    assert admin.patch('/api/v1/devices/' + payload['id'], json={'name': 'Workshop', 'tags': ['CNC']}).status_code == 200
    assert admin.get('/api/v1/inventory').json['devices'][0]['name'] == 'Workshop'
    assert admin.patch('/api/v1/devices/' + payload['id'], json={'revoke': True}).status_code == 200
    assert pi.post('/api/v1/device/check-in', json=payload).status_code == 401


def test_snapshot_group_and_revision(app, admin):
    _, one, _ = enroll(app, admin)
    _, two, _ = enroll(app, admin, 'serial-2')
    c = admin.post('/api/v1/collections', json={'name': 'Jobs'}).json['id']
    entry = upload(admin)
    assert admin.put('/api/v1/sets/collections/' + c, json={'revision': 0, 'manifest': [entry]}).status_code == 200
    assert admin.put('/api/v1/sets/collections/' + c, json={'revision': 0, 'manifest': []}).status_code == 409
    group = admin.post('/api/v1/groups', json={'name': 'Machines', 'devices': [one['id']]}).json['id']
    review = admin.post('/api/v1/reviews', json={'groups': [group], 'collection': c}).json
    admin.post('/api/v1/groups', json={'id': group, 'name': 'Machines', 'devices': [two['id']]})
    admin.put('/api/v1/sets/collections/' + c, json={'revision': 1, 'manifest': []})
    for _ in range(2):
        assert admin.post('/api/v1/reviews/' + review['id'] + '/deploy', json={}).status_code in (200, 201)
    jobs = admin.get('/api/v1/deployments').json['deployments']
    assert len(jobs) == 1 and jobs[0]['device_id'] == one['id']
    assert admin.get('/api/v1/deployments/' + jobs[0]['id'] + '/manifest').json['manifest'] == [entry]


def test_pull_hold_resume_ranges_and_lost_ack(app, admin):
    pi, payload, _ = enroll(app, admin)
    other, other_payload, _ = enroll(app, admin, 'serial-2')
    entry = upload(admin)
    job = deploy(admin, payload, [entry], 'hold')
    assignment = pi.post('/api/v1/device/check-in', json=payload).json['assignment']
    assert assignment['id'] == job['id'] and not assignment['activate']
    assert other.get('/api/v1/device/content/' + entry['sha256']).status_code == 403
    chunk = pi.get('/api/v1/device/content/' + entry['sha256'], headers={'Range': 'bytes=2-'})
    assert chunk.status_code == 206 and chunk.data == b'llo'
    payload['report'] = {'id': job['id'], 'state': 'prepared'}
    assert not pi.post('/api/v1/device/check-in', json=payload).json['assignment']['activate']
    admin.post('/api/v1/deployments/' + job['id'] + '/approve', json={})
    # Repeat prepared after losing the authorization response.
    for _ in range(2):
        r = pi.post('/api/v1/device/check-in', json=payload)
        assert r.status_code == 200 and r.json['assignment']['activate']
    payload['report']['state'] = 'succeeded'
    payload['telemetry']['active_deployment'] = job['id']
    for _ in range(2):
        assert pi.post('/api/v1/device/check-in', json=payload).json['assignment'] is None


def test_pause_cancel_retry_local_and_schedule(app, admin):
    pi, payload, _ = enroll(app, admin)
    job = deploy(admin, payload, [], 'scheduled', due=time.time() + 3600)
    admin.patch('/api/v1/devices/' + payload['id'], json={'paused': True})
    assert pi.post('/api/v1/device/check-in', json=payload).json['assignment'] is None
    admin.patch('/api/v1/devices/' + payload['id'], json={'paused': False})
    assert pi.post('/api/v1/device/check-in', json=payload).json['assignment']
    payload['report'] = {'id': job['id'], 'state': 'prepared'}
    assert not pi.post('/api/v1/device/check-in', json=payload).json['assignment']['activate']
    admin.post('/api/v1/deployments/' + job['id'] + '/cancel', json={})
    assert pi.post('/api/v1/device/check-in', json=payload).json['assignment']['cancel']
    payload['report']['state'] = 'canceled'
    pi.post('/api/v1/device/check-in', json=payload)
    assert admin.post('/api/v1/deployments/' + job['id'] + '/retry', json={}).status_code == 200
    payload['telemetry']['local'] = True
    pi.post('/api/v1/device/check-in', json=payload)
    assert admin.post('/api/v1/reviews', json={'devices': [payload['id']]}).status_code == 400
    assert admin.post('/api/v1/reviews', json={'devices': [payload['id']], 'resume_local': True}).status_code == 201


def test_retention_protects_active_and_pinned(app, admin):
    _, payload, _ = enroll(app, admin)
    with app.extensions['db']() as db:
        jobs = []
        for i in range(14):
            row = Deployment(batch=str(uuid.uuid4()), device_id=payload['id'], manifest=[],
                             manifest_hash='a'*64, state='succeeded', created=time.time() + i, pinned=i == 1)
            db.add(row)
            jobs.append(row)
        db.flush()
        device = db.get(Device, payload['id'])
        device.telemetry = dict(device.telemetry, active_deployment=jobs[0].id)
        db.commit()
    collect(app)
    with app.extensions['db']() as db:
        rows = list(db.scalars(select(Deployment).order_by(Deployment.created)))
        assert rows[0].manifest == [] and rows[1].manifest == []
        assert rows[2].manifest is None and rows[3].manifest is None
        assert all(x.manifest == [] for x in rows[4:])


def test_window_overnight():
    window = {'timezone': 'UTC', 'days': [0], 'start': 1380, 'end': 60}
    assert in_window(window, datetime(2026, 9, 8, 0, 30, tzinfo=timezone.utc).timestamp())
    assert not in_window(window, datetime(2026, 9, 8, 2, 0, tzinfo=timezone.utc).timestamp())


def test_csrf_and_capacity(app, admin):
    _, payload, _ = enroll(app, admin)
    admin.environ_base['HTTP_X_CSRF_TOKEN'] = 'bad'
    assert admin.post('/api/v1/collections', json={'name': 'No'}).status_code == 400
    admin.environ_base['HTTP_X_CSRF_TOKEN'] = 'test'
    with app.extensions['db']() as db:
        d = db.get(Device, payload['id'])
        d.telemetry = {'capacity': 1}
        db.commit()
    entry = upload(admin)
    admin.put('/api/v1/sets/devices/' + payload['id'], json={'revision': 0, 'manifest': [entry]})
    assert admin.post('/api/v1/reviews', json={'devices': [payload['id']]}).status_code == 400
