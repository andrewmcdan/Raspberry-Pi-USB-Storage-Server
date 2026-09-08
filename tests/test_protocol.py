import sys
from pathlib import Path
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / 'opt' / 'piusb'))
from protocol import diff, manifest_hash, validate_manifest


def file(path='test.txt', size=1):
    return {'path': path, 'kind': 'file', 'size': size, 'sha256': 'a' * 64}


@pytest.mark.parametrize('path', ['../x', '/x', 'a//b', 'a/./b', 'CON.txt', 'x.', 'x ', 'a\\b', 'x:y', 'a\x00b'])
def test_bad_paths(path):
    with pytest.raises(ValueError):
        validate_manifest([file(path)])


def test_manifest_identity_and_capacity():
    rows = [{'path': 'empty', 'kind': 'dir'}, file()]
    assert manifest_hash(rows) == manifest_hash(list(reversed(rows)))
    with pytest.raises(ValueError):
        validate_manifest(rows, 0)
    with pytest.raises(ValueError):
        validate_manifest([file(size=2**32)])
    with pytest.raises(ValueError):
        validate_manifest([file(), file('TEST.txt')])
    with pytest.raises(ValueError):
        validate_manifest([file('missing/child')])
    assert diff([file()], [])['deletions'] == ['test.txt']
