import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def local(tmp_path, monkeypatch):
    config = tmp_path / 'piusb.ini'
    config.write_text('[piusb]\n' + '\n'.join(f'{k}_dir = {tmp_path / k}' for k in ('staging', 'incoming', 'state', 'runtime')) + '\nimage_size_mib = 64\n')
    web = tmp_path / 'web.ini'
    web.write_text('[web]\nusername = admin\npassword_hash = ' + generate_password_hash('test') + '\nsecret_key = test-secret\n')
    monkeypatch.setenv('PIUSB_CONFIG', str(config))
    monkeypatch.setenv('PIUSB_WEB_CONFIG', str(web))
    spec = importlib.util.spec_from_file_location('local_web', Path(__file__).resolve().parents[1] / 'opt/piusb/web/app.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'local_web', module)
    spec.loader.exec_module(module)
    module.app.testing = True
    client = module.app.test_client()
    with client.session_transaction() as s:
        s.update(authenticated=True, csrf_token='test')
    return module, client


def test_standalone_upload_and_publish_request(local):
    module, client = local
    assert client.get('/').status_code == 200
    result = client.post('/api/uploads/file', data={'csrf_token': 'test', 'file': (io.BytesIO(b'hello'), 'hello.txt')})
    assert result.status_code == 201
    assert (module.STAGING_DIR / 'hello.txt').read_bytes() == b'hello'
    assert client.post('/publish', data={'csrf_token': 'test'}).status_code == 302
    assert module.PUBLISH_REQUEST.exists()


def test_managed_mutations_blocked_and_takeover_queued(local):
    module, client = local
    control = module.STATE_DIR / 'fleet/control.json'
    control.parent.mkdir(parents=True)
    control.write_text(json.dumps({'mode': 'managed'}))
    assert b'Fleet control: managed' in client.get('/').data
    result = client.post('/api/uploads/file', data={'csrf_token': 'test', 'file': (io.BytesIO(b'hello'), 'hello.txt')})
    assert result.status_code == 409
    assert not (module.STAGING_DIR / 'hello.txt').exists()
    assert client.post('/publish', data={'csrf_token': 'test'}).status_code == 409
    assert client.post('/local-takeover', data={'csrf_token': 'test'}).status_code == 302
    queued = json.loads((module.RUNTIME_DIR / 'takeover.request').read_text())
    assert queued['operation'] == 'takeover'
    with pytest.raises(RuntimeError, match='Central manager'):
        with module.staging_lock(blocking=False):
            pass
