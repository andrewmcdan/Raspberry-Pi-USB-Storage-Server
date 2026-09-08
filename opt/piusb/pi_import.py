"""Upload a root-owned Pi snapshot through the existing outbound connection."""
import time
from agent import read, write


def transfer(agent, task):
    key = task['id']
    result = read(agent.state_dir / 'pi-export.json')
    if result.get('id') != key:
        failed = read(agent.state_dir / 'fleet-result.json')
        if failed.get('request_id') == key and failed.get('state') == 'failed':
            response = agent.http.post(agent.config['url'] + '/api/v1/device/snapshots/' + key + '/manifest',
                                       json={'error': failed.get('error', 'Snapshot failed')}, timeout=30)
            response.raise_for_status()
            return
        if not (agent.runtime / 'fleet.request').exists():
            # Root bridge is serialized with builds and USB switching. Repeated
            # requests for this id reuse its durable result.
            write(agent.runtime / 'fleet.request', {'operation': 'export',
                'request_id': key, 'source': task['source']}, 0o640)
        return
    base = agent.config['url'] + '/api/v1/device/snapshots/' + key
    if result['state'] == 'failed':
        response = agent.http.post(base + '/manifest', json={'error': result['error']}, timeout=30)
        response.raise_for_status()
        return
    response = agent.http.post(base + '/manifest', json={'manifest': result['manifest']}, timeout=30)
    response.raise_for_status()
    for item in response.json()['missing']:
        with (agent.state_dir / 'pi-export' / item['sha256']).open('rb') as stream:
            offset = item['offset']
            stream.seek(offset)
            while offset < item['size'] or item['size'] == 0:
                chunk = stream.read(8 * 1024**2)
                response = agent.http.put(base + '/content/' + item['sha256'],
                    params={'offset': offset}, data=chunk, timeout=120)
                response.raise_for_status()
                offset = response.json()['offset']
                if time.monotonic() - agent.last_poll > 10:
                    agent.poll()
                if item['size'] == 0:
                    break
    response = agent.http.post(base + '/complete', json={}, timeout=30)
    response.raise_for_status()
