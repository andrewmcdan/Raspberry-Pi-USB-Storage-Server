#!/usr/bin/env python3
"""Outbound-only Pi USB fleet agent. Root operations use the fixed systemd bridge."""
import argparse
import configparser
import hashlib
import json
import os
import random
import shutil
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

from protocol import digest_file, manifest_hash, validate_manifest

CONFIG = Path(os.environ.get('PIUSB_AGENT_CONFIG', '/etc/piusb/agent.json'))


def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    with temp.open('x') as stream:
        os.chmod(temp, mode)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def serial():
    for path in ('/sys/firmware/devicetree/base/serial-number', '/proc/device-tree/serial-number'):
        if Path(path).exists():
            return Path(path).read_text().strip('\x00\n ')
    raise RuntimeError('Pi hardware serial unavailable; enrollment requires real hardware identity')


class Paused(Exception):
    pass


class Canceled(Exception):
    pass


class Agent:
    def __init__(self, config, settings=None):
        self.config = config
        if urlparse(config['url']).scheme != 'https' and not config.get('test_http'):
            raise ValueError('Manager URL must use HTTPS')
        parser = configparser.ConfigParser()
        parser.read(os.environ.get('PIUSB_CONFIG', '/etc/piusb/piusb.ini'))
        self.settings = settings or dict(parser['piusb'])
        self.data = Path(self.settings.get('staging_dir', '/srv/piusb/staging')).parent / 'agent'
        self.state_dir = Path(self.settings.get('state_dir', '/var/lib/piusb'))
        self.runtime = Path(self.settings.get('runtime_dir', '/run/piusb'))
        self.state_file = self.data / 'state.json'
        self.state = read(self.state_file)
        self.http = requests.Session()
        self.http.headers['Authorization'] = 'Bearer ' + config['credential']
        self.http.verify = config.get('ca_bundle', True)
        if self.http.verify is False:
            raise ValueError('TLS certificate verification cannot be disabled')
        self.data.mkdir(parents=True, exist_ok=True)
        self.last_poll = 0
        self.response = {}

    def save(self):
        write(self.state_file, self.state)

    def telemetry(self):
        status = read(self.state_dir / 'status.json')
        control = read(self.state_dir / 'fleet' / 'control.json')
        try:
            ips = sorted({x[4][0] for x in socket.getaddrinfo(socket.gethostname(), None)})
        except socket.gaierror:
            ips = []
        return {'hostname': socket.gethostname(), 'ips': ips, 'version': '2.1.0',
                'capacity': int(self.settings.get('image_size_mib', 4096)) * 1024**2 *
                            int(self.settings.get('staging_fill_percent', 90)) // 100,
                'free_bytes': shutil.disk_usage(self.data).free,
                'usb_state': status.get('state', 'unknown'),
                'active_deployment': status.get('active_deployment'),
                'prepared_deployment': status.get('prepared_deployment'),
                'local': control.get('mode') == 'local', 'paused': self.state.get('paused', False)}

    def observe(self):
        job = self.state.get('job')
        if not job or job['state'] in ('succeeded', 'failed', 'canceled'):
            return
        row = read(self.state_dir / 'fleet' / (job['id'] + '.json'))
        if row:
            state = {'activating': 'switching'}.get(row['state'], row['state'])
            # A durable prepared record stays prepared until the activation request is consumed.
            if job['state'] == 'switching' and state == 'prepared':
                return
            job.update(state=state, error=row.get('error', ''))
        result = read(self.state_dir / 'fleet-result.json')
        if result.get('request_id') == job.get('request_id') and result.get('state') == 'failed':
            job.update(state='failed', error=result.get('error', 'Root operation failed'))
        self.save()

    def poll(self):
        self.observe()
        job = self.state.get('job')
        payload = {'id': self.config['id'], 'serial': self.config['serial'], 'telemetry': self.telemetry()}
        if job:
            payload['report'] = {k: job[k] for k in ('id', 'state', 'progress', 'error') if k in job}
        response = self.http.post(self.config['url'] + '/api/v1/device/check-in', json=payload, timeout=30)
        response.raise_for_status()
        self.response = response.json()
        self.state['paused'] = self.response['paused']
        self.last_poll = time.monotonic()
        self.save()
        return self.response

    def checkpoint(self, force=False):
        if force or time.monotonic() - self.last_poll >= 10:
            self.poll()
        assignment = self.response.get('assignment')
        if assignment and assignment['id'] == self.state['job']['id'] and assignment['cancel']:
            raise Canceled()
        if self.state.get('paused') or self.telemetry()['local'] and not self.state['job'].get('resume_local'):
            raise Paused()

    def download(self, item, cache):
        final = cache / item['sha256']
        if final.exists():
            if final.stat().st_size == item['size'] and digest_file(final) == item['sha256']:
                return final
            final.unlink()
        partial = cache / (item['sha256'] + '.part')
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > item['size']:
            partial.unlink()
            offset = 0
        if offset < item['size'] or not partial.exists():
            headers = {'Range': f'bytes={offset}-', 'If-Range': '"' + item['sha256'] + '"'} if offset else {}
            with self.http.get(self.config['url'] + '/api/v1/device/content/' + item['sha256'],
                               headers=headers, stream=True, timeout=(15, 30)) as response:
                response.raise_for_status()
                if offset and response.status_code == 200:
                    offset = 0
                if response.status_code == 206 and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise ValueError('Invalid content range')
                with partial.open('ab' if offset else 'wb') as stream:
                    for chunk in response.iter_content(1024**2):
                        if offset + len(chunk) > item['size']:
                            raise ValueError('Download exceeded manifest size')
                        stream.write(chunk)
                        offset += len(chunk)
                        self.state['job']['progress'] = {'message': item['path'], 'bytes': offset, 'total': item['size']}
                        self.checkpoint()
                    stream.flush()
                    os.fsync(stream.fileno())
        if partial.stat().st_size != item['size'] or digest_file(partial) != item['sha256']:
            partial.unlink(missing_ok=True)
            raise ValueError('Downloaded file failed SHA-256 verification')
        os.replace(partial, final)
        return final

    def prepare_files(self, assignment):
        manifest = assignment['manifest']
        total = validate_manifest(manifest, self.telemetry()['capacity'])
        if manifest_hash(manifest) != assignment['hash']:
            raise ValueError('Manifest hash mismatch')
        cache = self.data / 'cache'
        cache.mkdir(exist_ok=True)
        wanted = {x['sha256'] for x in manifest if x['kind'] == 'file'}
        # Single executing deployment: cached blobs outside the new snapshot can be reclaimed.
        for path in cache.iterdir():
            if path.name.removesuffix('.part') not in wanted and path.is_file():
                path.unlink()
        candidates = self.data / 'candidates'
        candidates.mkdir(exist_ok=True)
        for path in candidates.iterdir():
            if path.name != assignment['id'] and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        missing = sum(x['size'] for x in manifest if x['kind'] == 'file' and not (cache / x['sha256']).exists())
        # Root later requires a verified copy and full image headroom as well.
        if shutil.disk_usage(self.data).free < missing + total + int(self.settings.get('image_size_mib', 4096)) * 1024**2 + 64 * 1024**2:
            raise ValueError('Insufficient free disk space for download, snapshot and image')
        candidate = candidates / assignment['id']
        files = candidate / 'files'
        files.mkdir(parents=True, exist_ok=True)
        for item in sorted(manifest, key=lambda x: (x['path'].count('/'), x['path'])):
            self.checkpoint()
            target = files / item['path']
            if item['kind'] == 'dir':
                target.mkdir(exist_ok=True)
            else:
                content = self.download(item, cache)
                if not target.exists():
                    os.link(content, target)
        write(candidate / 'manifest.json', manifest)
        self.checkpoint(force=True)

    def enqueue(self, operation):
        job = self.state['job']
        # Stable request ID permits retries after a crash between journaling and enqueue.
        request_id = job.setdefault(operation + '_request', str(uuid.uuid4()))
        job['request_id'] = request_id
        job['state'] = 'building' if operation == 'prepare' else 'switching'
        self.save()
        if (self.runtime / 'fleet.request').exists():
            return
        write(self.runtime / 'fleet.request', {'request_id': request_id, 'operation': operation,
              'deployment': job['id'], 'hash': job['hash'], 'resume_local': job.get('resume_local', False)}, 0o640)

    def tick(self):
        response = self.poll()
        assignment = response.get('assignment')
        if response.get('snapshot') and not assignment:
            from pi_import import transfer
            try:
                transfer(self, response['snapshot'])
            except requests.HTTPError as error:
                if error.response.status_code not in (403, 409):
                    raise
                # A canceled import or lost chunk acknowledgment is reconciled next poll.
            except (OSError, ValueError) as error:
                failed = self.http.post(self.config['url'] + '/api/v1/device/snapshots/' + response['snapshot']['id'] + '/manifest',
                                        json={'error': str(error)}, timeout=30)
                failed.raise_for_status()
            return
        if not assignment:
            return
        job = self.state.get('job')
        if not job or job['id'] != assignment['id']:
            job = {'id': str(uuid.UUID(assignment['id'])), 'state': 'downloading',
                   'hash': assignment['hash'], 'resume_local': assignment['resume_local']}
            self.state['job'] = job
            self.save()
        if job['state'] in ('succeeded', 'failed', 'canceled'):
            return
        # Never cancel or pause an activation already authorized and journaled.
        if job['state'] == 'switching' or (job['state'] == 'prepared' and assignment['activate']):
            self.enqueue('activate')
            return
        if assignment['cancel'] and job['state'] != 'building':
            job['state'] = 'canceled'
            self.save()
            return
        if response['paused'] or (self.telemetry()['local'] and not job['resume_local']):
            return
        try:
            if job['state'] == 'downloading':
                self.prepare_files(assignment)
                self.enqueue('prepare')
            elif job['state'] == 'building':
                self.enqueue('prepare')
            elif job['state'] == 'prepared' and assignment['activate']:
                self.enqueue('activate')
        except Paused:
            pass
        except Canceled:
            job['state'] = 'canceled'
        except requests.HTTPError as error:
            if error.response.status_code >= 500 or error.response.status_code in (408, 429):
                raise
            job.update(state='failed', error=f'Download rejected with HTTP {error.response.status_code}')
        except requests.RequestException:
            raise
        except (ValueError, OSError) as error:
            job.update(state='failed', error=str(error))
        self.save()


def enroll(args):
    if os.geteuid() != 0:
        raise RuntimeError('Enrollment must run as root')
    if urlparse(args.url).scheme != 'https':
        raise ValueError('Enrollment requires HTTPS')
    identity_path = CONFIG.with_name('device-id')
    if identity_path.exists():
        identity = identity_path.read_text().strip()
    else:
        identity = str(uuid.uuid4())
        identity_path.write_text(identity)
        os.chmod(identity_path, 0o644)
    hardware = serial()
    old = read(CONFIG)
    if old and old.get('serial') != hardware:
        raise RuntimeError('Stored identity belongs to different hardware; do not clone enrolled SD images')
    response = requests.post(args.url.rstrip('/') + '/api/v1/enroll',
                             json={'token': args.token, 'id': identity, 'serial': hardware,
                                   'hostname': socket.gethostname()}, verify=args.ca_bundle or True, timeout=30)
    response.raise_for_status()
    config = dict(response.json(), serial=hardware, url=args.url.rstrip('/'), ca_bundle=args.ca_bundle or True)
    write(CONFIG, config, 0o640)
    import grp
    os.chown(CONFIG, 0, grp.getgrnam('piusb').gr_gid)
    import manager as m
    settings = m.load_settings()
    with m.exclusive_lock(settings.manager_lock), m.exclusive_lock(settings.staging_lock):
        write(settings.state_dir / 'fleet' / 'control.json', {'mode': 'managed'}, 0o644)
    print('Enrolled ' + identity + '. Run: sudo systemctl restart piusb-agent.service')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'enroll'])
    parser.add_argument('--url')
    parser.add_argument('--token')
    parser.add_argument('--ca-bundle')
    args = parser.parse_args()
    if args.command == 'enroll':
        if not args.url or not args.token:
            parser.error('enroll requires --url and --token')
        enroll(args)
        return
    import fcntl
    agent = Agent(read(CONFIG))
    # Fail closed on cloned credentials even before a check-in.
    if serial() != agent.config['serial']:
        raise RuntimeError('Agent hardware identity mismatch')
    with (agent.data / 'agent.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        failures = 0
        while True:
            try:
                agent.tick()
                failures = 0
            except requests.HTTPError as error:
                code = error.response.status_code
                if code in (401, 403, 409):
                    raise RuntimeError(f'Manager rejected agent ({code}); operator intervention required') from error
                print(f'HTTP check-in/download failure: {code}', flush=True)
                failures += 1
            except requests.RequestException as error:
                print(f'Network failure: {type(error).__name__}', flush=True)
                failures += 1
            time.sleep(min(300, 15 * 2**min(failures, 5)) * random.uniform(.9, 1.1))


if __name__ == '__main__':
    main()
