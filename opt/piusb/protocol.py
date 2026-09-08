"""Shared, dependency-free validation for manager manifests and Pi agents."""
import hashlib
import json
import re
import unicodedata
from pathlib import PurePosixPath

MAX_FILE = 2**32 - 1
RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{n}' for p in ('COM', 'LPT') for n in range(1, 10)}


def valid_path(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError('Invalid path')
    if value != PurePosixPath(value).as_posix() or value.startswith('/'):
        raise ValueError('Path must be a canonical relative path')
    for part in value.split('/'):
        if (part in ('.', '..', '') or part.endswith((' ', '.')) or
                len(part.encode('utf-16-le')) // 2 > 240 or
                part.split('.')[0].upper() in RESERVED or
                any(ord(c) < 32 or c in '<>:"\\|?*' for c in part)):
            raise ValueError(f'Unsafe FAT32 path: {value}')
    return value


def validate_manifest(manifest, capacity=None):
    if not isinstance(manifest, list) or len(manifest) > 100000:
        raise ValueError('Manifest must be a list with at most 100000 entries')
    names, total = {}, 0
    for item in manifest:
        if not isinstance(item, dict):
            raise ValueError('Invalid manifest entry')
        path = valid_path(item.get('path'))
        kind = item.get('kind')
        if kind not in ('file', 'dir'):
            raise ValueError('Invalid entry kind')
        folded = unicodedata.normalize('NFC', path).casefold()
        if folded in names:
            raise ValueError(f'Case-insensitive path collision: {path}')
        names[folded] = item
        if kind == 'file':
            size = item.get('size')
            if type(size) is not int or not 0 <= size <= MAX_FILE:
                raise ValueError('File exceeds FAT32 limits')
            if not re.fullmatch('[0-9a-f]{64}', item.get('sha256', '')):
                raise ValueError('Invalid SHA-256')
            total += size
    for item in manifest:
        for parent in PurePosixPath(item['path']).parents:
            if str(parent) == '.':
                continue
            key = unicodedata.normalize('NFC', str(parent)).casefold()
            if key not in names or names[key]['kind'] != 'dir' or names[key]['path'] != str(parent):
                raise ValueError(f'Missing or conflicting parent directory: {parent}')
    if capacity is not None and total > capacity:
        raise ValueError(f'Contents require {total} bytes; safe capacity is {capacity}')
    return total


def manifest_hash(manifest):
    validate_manifest(manifest)
    return hashlib.sha256(json.dumps(sorted(manifest, key=lambda x: x['path']),
                                     sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def digest_file(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def diff(old, new):
    before, after = ({x['path']: x for x in rows} for rows in (old, new))
    return {'additions': sorted(after.keys() - before.keys()),
            'deletions': sorted(before.keys() - after.keys()),
            'replacements': sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}
