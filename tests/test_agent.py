import hashlib
import json
from pathlib import Path
import requests
import pytest

from agent import Agent, Canceled


class Response:
    status_code = 206
    def __init__(self, content, offset=2):
        self.content = content
        self.headers = {'Content-Range': f'bytes {offset}-4/5'}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def raise_for_status(self):
        pass
    def iter_content(self, size):
        yield self.content


def agent(tmp_path):
    return Agent({'url': 'https://example.test', 'credential': 'test', 'id': 'id', 'serial': 'serial'},
                 {'staging_dir': str(tmp_path / 'staging'), 'state_dir': str(tmp_path / 'state'),
                  'runtime_dir': str(tmp_path / 'run'), 'image_size_mib': '64'})


def test_download_resume_and_hash(tmp_path, monkeypatch):
    a = agent(tmp_path)
    a.state['job'] = {'id': 'x'}
    cache = tmp_path / 'cache'
    cache.mkdir()
    item = {'path': 'x', 'size': 5, 'sha256': hashlib.sha256(b'hello').hexdigest()}
    (cache / (item['sha256'] + '.part')).write_bytes(b'he')
    seen = []
    def get(url, **kwargs):
        seen.append(kwargs['headers']['Range'])
        return Response(b'llo')
    monkeypatch.setattr(a.http, 'get', get)
    monkeypatch.setattr(a, 'checkpoint', lambda **kwargs: None)
    assert a.download(item, cache).read_bytes() == b'hello'
    assert seen == ['bytes=2-']
    assert a.download(item, cache).read_bytes() == b'hello'
    assert len(seen) == 1


def test_download_bad_hash_and_network(tmp_path, monkeypatch):
    a = agent(tmp_path)
    a.state['job'] = {'id': 'x'}
    cache = tmp_path / 'cache'
    cache.mkdir()
    item = {'path': 'x', 'size': 5, 'sha256': hashlib.sha256(b'hello').hexdigest()}
    monkeypatch.setattr(a.http, 'get', lambda *args, **kwargs: Response(b'wrong', offset=0))
    monkeypatch.setattr(a, 'checkpoint', lambda **kwargs: None)
    with pytest.raises(ValueError, match='SHA-256'):
        a.download(item, cache)
    assert not (cache / (item['sha256'] + '.part')).exists()
    def failure(*args, **kwargs):
        raise requests.ConnectionError('offline')
    monkeypatch.setattr(a.http, 'get', failure)
    with pytest.raises(requests.ConnectionError):
        a.download(item, cache)


def test_no_tls_bypass(tmp_path):
    with pytest.raises(ValueError, match='HTTPS'):
        Agent({'url': 'http://example.test'}, {})


def test_cancellation_checkpoint_stops_download(tmp_path, monkeypatch):
    a = agent(tmp_path)
    a.state['job'] = {'id': 'test'}
    a.response = {'assignment': {'id': 'test', 'cancel': True}}
    monkeypatch.setattr(a, 'poll', lambda: a.response)
    with pytest.raises(Canceled):
        a.checkpoint(force=True)


def test_network_failure_preserves_downloading_job(tmp_path, monkeypatch):
    import uuid
    a = agent(tmp_path)
    assignment = {'id': str(uuid.uuid4()), 'hash': 'a'*64, 'resume_local': False, 'cancel': False}
    monkeypatch.setattr(a, 'poll', lambda: {'assignment': assignment, 'paused': False})
    def failure(_assignment):
        raise requests.ConnectionError('temporary outage')
    monkeypatch.setattr(a, 'prepare_files', failure)
    with pytest.raises(requests.ConnectionError):
        a.tick()
    assert a.state['job']['state'] == 'downloading'
