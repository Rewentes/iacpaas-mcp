"""A failed permissions setup must close the file before propagating the error."""

import errno
import os

import pytest

from standalone_agent import runtime


def test_permission_failure_closes_the_descriptor_and_preserves_the_error(tmp_path, monkeypatch):
    opened = []
    failure = PermissionError('Fixture permission error')

    def fail_permissions(fd, mode):
        opened.append(fd)
        raise failure

    monkeypatch.setattr(runtime, '_FCHMOD', fail_permissions)
    with pytest.raises(PermissionError) as result:
        runtime._private_fd(tmp_path / 'trace.jsonl', os.O_WRONLY | os.O_CREAT)
    assert result.value is failure
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF


def test_non_posix_permissions_fallback_keeps_the_returned_descriptor_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, '_FCHMOD', None)
    path = tmp_path / 'trace.jsonl'
    fd = runtime._private_fd(path, os.O_WRONLY | os.O_CREAT)
    try:
        os.write(fd, b'fixture')
    finally:
        os.close(fd)
    assert path.read_bytes() == b'fixture'
