"""Run against a disposable Compose stack: real HTTP/PostgreSQL, mocked USB only.

Invoke with docker compose run --rm manager python -m tests.simulate.
"""
import dataclasses
import tempfile
import time
import uuid
from pathlib import Path

import requests
from manager.app import create_app
from manager.models import Device
from tests.test_fleet import publisher
from agent import Agent, write
import fleet


def main():
    app = create_app()
    admin = app.test_client()
    with admin.session_transaction() as session:
        session.update(admin=True, csrf='simulation')
    admin.environ_base['HTTP_X_CSRF_TOKEN'] = 'simulation'
    base = 'http://manager:8000'
    marker = uuid.uuid4().hex[:8]
    publisher.CONFIG_PATH = Path('/app/etc/piusb/piusb.ini')
    publisher.require_root = lambda: None
    swaps = []
    fail_gadget = set()

    def build(s, target):
        if s.gadget_name in fail_gadget:
            raise RuntimeError('Simulated filesystem failure')
        publisher.validate_staging(s)
        target.write_bytes(b'simulated FAT image')
        return {'total_bytes': 5}

    def switch(s, old, new):
        swaps.append((s.gadget_name, new))
        publisher.write_active_slot(s, new)
        (s.lun_path / 'file').write_text(str(s.image_for_slot(new).resolve()))

    publisher.build_image, publisher.swap_gadget_image = build, switch
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        publisher.GADGETS_ROOT = root / 'gadgets'
        agents = []
        for i in range(2):
            identity, hardware = str(uuid.uuid4()), f'simulated-{marker}-{i}'
            token = admin.post('/api/v1/enrollment-tokens', json={}).json['token']
            response = requests.post(base + '/api/v1/enroll', json={'token': token, 'id': identity,
                                     'serial': hardware, 'hostname': f'Simulated workshop {i}'}, timeout=10)
            assert response.status_code == 201, response.text
            path = root / str(i)
            s = dataclasses.replace(publisher.load_settings(), image_dir=path / 'srv/images',
                                    staging_dir=path / 'srv/staging', state_dir=path / 'state',
                                    runtime_dir=path / 'run', mount_dir=path / 'mount',
                                    image_size_mib=64, gadget_name='sim' + str(i))
            publisher.ensure_directories(s)
            s.lun_path.mkdir(parents=True)
            (s.lun_path / 'ro').write_text('1')
            (s.gadget_path / 'UDC').write_text('dummy')
            (s.lun_path / 'file').write_text(str(s.image_a.resolve()))
            s.image_a.write_bytes(b'old image')
            publisher.write_active_slot(s, 'A')
            config = dict(response.json(), serial=hardware, url=base, test_http=True)
            settings = {k: str(v) for k, v in dataclasses.asdict(s).items()}
            a = Agent(config, settings)
            a.tick()
            agents.append((a, s))
        entry = admin.post('/api/v1/content', data=b'hello').json | {'kind': 'file', 'path': 'job.txt'}
        collection = admin.post('/api/v1/collections', json={'name': 'Simulation ' + marker}).json['id']
        admin.put('/api/v1/sets/collections/' + collection, json={'revision': 0, 'manifest': [entry]})
        ids = [a.config['id'] for a, _ in agents]

        def deploy(policy):
            response = admin.post('/api/v1/reviews', json={'devices': ids, 'collection': collection, 'policy': policy})
            assert response.status_code == 201, response.json
            batch = response.json['id']
            assert admin.post('/api/v1/reviews/' + batch + '/deploy', json={}).status_code == 201
            return batch

        def step(pair):
            a, s = pair
            a.tick()
            if (s.runtime_dir / 'fleet.request').exists():
                fleet.consume(s)

        def jobs(batch):
            return [d for d in admin.get('/api/v1/deployments').json['deployments'] if d['batch'] == batch]

        batch = deploy('hold')
        for _ in range(3):
            step(agents[0])
        assert not swaps
        assert {d['state'] for d in jobs(batch)} == {'prepared', 'queued'}
        with app.extensions['db']() as db:
            db.get(Device, ids[1]).last_seen = time.time() - 61
            db.commit()
        assert not next(d for d in admin.get('/api/v1/inventory').json['devices'] if d['id'] == ids[1])['online']
        for _ in range(3):
            step(agents[1])
        for d in jobs(batch):
            admin.post('/api/v1/deployments/' + d['id'] + '/approve', json={})
        for _ in range(3):
            for pair in agents:
                step(pair)
        assert all(d['state'] == 'succeeded' for d in jobs(batch)), jobs(batch)
        assert len(swaps) == 2
        # Restart an agent after completion; acknowledgment must not cause another switch.
        old, s = agents[0]
        agents[0] = Agent(old.config, old.settings), s
        step(agents[0])
        assert len(swaps) == 2
        batch = deploy('auto')
        fail_gadget.add('sim1')
        for _ in range(5):
            for pair in agents:
                step(pair)
        assert {d['state'] for d in jobs(batch)} == {'succeeded', 'failed'}, jobs(batch)
        assert len(swaps) == 3
        print('PASS: two actual agents / HTTP / PostgreSQL; offline catch-up, hold, approval, restart, partial fleet failure; USB operations mocked.')


if __name__ == '__main__':
    main()
