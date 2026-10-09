"""Read files an agent controls without blocking on a FIFO or reading a device.

Evidence files, worktree files and transcripts live where the agent can replace them
at any moment. Opening with O_NONBLOCK returns at once even for a FIFO, and the type
check runs on the opened descriptor, so a path swapped after an earlier is_file()
check is still rejected. A rejected path raises OSError, as a missing file does.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat


def open_regular(path, mode: str = "rb", encoding: str | None = None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", os.fspath(path))
        os.set_blocking(descriptor, True)
        return os.fdopen(descriptor, mode, encoding=encoding)
    except BaseException:
        os.close(descriptor)
        raise


def read_regular(path) -> bytes:
    with open_regular(path) as stream:
        return stream.read()


def sha256_regular(path) -> str:
    digest = hashlib.sha256()
    with open_regular(path) as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
