import hashlib
import time
from pathlib import Path
from tests.test_manager import app, admin, enroll
from manager.worker import collect


def test_snapshot_resume_isolation_and_draft(app, admin):
    pi, payload, _ = enroll(app, admin)
    other, _, _ = enroll(app, admin, 'other')
    base = '/api/v1/devices/' + payload['id'] + '/snapshots'
    key = admin.post(base, json={'source': 'active'}).json['id']
    assert admin.post(base, json={'source': 'staging'}).status_code == 409
    assert pi.post('/api/v1/device/check-in', json=payload).json['snapshot']['id'] == key
    endpoint = '/api/v1/device/snapshots/' + key
    blob = b'existing USB content'
    digest = hashlib.sha256(blob).hexdigest()
    manifest = [{'path': 'empty', 'kind': 'dir'}, {'path': 'print.gcode', 'kind': 'file', 'size': len(blob), 'sha256': digest}]
    assert other.post(endpoint + '/manifest', json={'manifest': manifest}).status_code == 403
    assert pi.post(endpoint + '/manifest', json={'manifest': manifest}).json['missing'][0]['offset'] == 0
    assert pi.put(endpoint + '/content/' + digest + '?offset=0', data=blob[:4]).status_code == 200
    assert pi.post(endpoint + '/manifest', json={'manifest': manifest}).json['missing'][0]['offset'] == 4
    assert pi.put(endpoint + '/content/' + digest + '?offset=0', data=blob).status_code == 409
    assert pi.put(endpoint + '/content/' + digest + '?offset=4', data=blob[4:]).status_code == 200
    for _ in range(2):
        assert pi.post(endpoint + '/complete', json={}).status_code == 200
    assert admin.get(base).json['snapshots'][0]['state'] == 'ready'
    assert admin.post(base + '/' + key + '/draft', json={'revision': 1}).status_code == 409
    assert admin.post(base + '/' + key + '/draft', json={'revision': 0}).status_code == 200
    assert admin.get('/api/v1/sets/devices/' + payload['id']).json['manifest'] == manifest
    collect(app, time.time() + 90000)
    assert admin.get('/api/v1/content/' + digest).data == blob


def test_snapshot_rejects_paths_hash_and_mutation(app, admin):
    pi, payload, _ = enroll(app, admin)
    key = admin.post('/api/v1/devices/' + payload['id'] + '/snapshots', json={'source': 'staging'}).json['id']
    base = '/api/v1/device/snapshots/' + key
    assert pi.post(base + '/manifest', json={'manifest': [{'kind': 'dir', 'path': '../escape'}]}).status_code == 400
    manifest = [{'kind': 'file', 'path': 'x', 'size': 3, 'sha256': hashlib.sha256(b'yes').hexdigest()}]
    assert pi.post(base + '/manifest', json={'manifest': manifest}).status_code == 200
    assert pi.post(base + '/manifest', json={'manifest': []}).status_code == 409
    assert pi.put(base + '/content/' + manifest[0]['sha256'] + '?offset=0', data=b'bad').status_code == 400
    assert pi.post(base + '/manifest', json={'manifest': manifest}).json['missing'][0]['offset'] == 0
    assert pi.post(base + '/complete', json={}).status_code == 400


def test_preview_requires_login_and_uses_imported_blob(app, admin):
    from tests.test_manager import upload
    item = upload(admin, b'G1 X1 E1', 'part.gcode')
    url = '/preview/' + item['sha256'] + '/part.gcode'
    assert app.test_client().get(url).status_code == 401
    page = admin.get(url)
    assert page.status_code == 200
    assert ('/api/v1/content/' + item['sha256']).encode() in page.data
    assert admin.get('/preview-assets/worker.js').status_code == 200


def test_agent_import_uploads_and_recovers_lost_chunk_ack(app, admin, tmp_path):
    import json
    import requests
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from pi_import import transfer
    pi, payload, _ = enroll(app, admin)
    key = admin.post('/api/v1/devices/' + payload['id'] + '/snapshots', json={'source': 'active'}).json['id']
    blob = b'G1 X1'
    digest = hashlib.sha256(blob).hexdigest()
    manifest = [{'path': 'x.gcode', 'kind': 'file', 'size': len(blob), 'sha256': digest}]
    state = tmp_path / 'agent-state'; (state / 'pi-export').mkdir(parents=True)
    (state / 'pi-export' / digest).write_bytes(blob)
    (state / 'pi-export.json').write_text(json.dumps({'id':key,'state':'ready','manifest':manifest}))
    class HTTP:
        lost = True
        def send(self, method, url, **kwargs):
            kwargs.pop('timeout', None)
            params = kwargs.pop('params', None)
            r = pi.open(urlsplit(url).path, method=method, query_string=params, **kwargs)
            if method == 'PUT' and self.lost:
                self.lost = False
                raise requests.ConnectionError('Lost acknowledgment')
            def check():
                assert r.status_code < 300, r.json
            return SimpleNamespace(json=lambda:r.json, raise_for_status=check)
        def post(self, url, **kwargs): return self.send('POST', url, **kwargs)
        def put(self, url, **kwargs): return self.send('PUT', url, **kwargs)
    agent = SimpleNamespace(state_dir=state, runtime=tmp_path, config={'url':'https://example'}, http=HTTP(), last_poll=time.monotonic(), poll=lambda:None)
    import pytest
    with pytest.raises(requests.ConnectionError): transfer(agent, {'id':key,'source':'active'})
    transfer(agent, {'id':key,'source':'active'})
    assert admin.get('/api/v1/devices/' + payload['id'] + '/snapshots').json['snapshots'][0]['state']=='ready'
