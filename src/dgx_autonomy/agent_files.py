"""Reading files the agent controls, from the host, as root, without being tricked.

The agent owns its workspace (`runs/<id>/agent`) and can put symlinks anywhere in
it. The controller sees the whole data directory at the same path, so following
`agent/.dgx -> /var/lib/dgx-autonomy/state` would read controller state. Every
component below the workspace root is therefore opened with O_NOFOLLOW relative to
its parent's descriptor, and only regular files of bounded size are read. The
workspace root itself is created by the controller inside a root-owned directory,
so the agent cannot replace it.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class AgentFileError(ValueError):
    """The path exists but is not a plain file or directory we are willing to read."""


def _open_dir(parent: int, name: str) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP (a symlink), ENOTDIR, EACCES
        raise AgentFileError(f"{name}: not a directory we can read ({exc.strerror})") from None


@contextmanager
def open_dir(root: Path, *parts: str) -> Iterator[int]:
    """A descriptor for root/parts..., no symlink followed below `root`."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            nxt = _open_dir(fd, part)
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def read_bytes_at(dir_fd: int, name: str, max_bytes: int) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AgentFileError(f"{name}: not a file we can read ({exc.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AgentFileError(f"{name}: not a regular file")
        if st.st_size > max_bytes:
            raise AgentFileError(f"{name}: larger than {max_bytes} bytes")
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise AgentFileError(f"{name}: larger than {max_bytes} bytes")
        return data
    finally:
        os.close(fd)


def read_json_at(dir_fd: int, name: str, max_bytes: int) -> Any:
    try:
        return json.loads(read_bytes_at(dir_fd, name, max_bytes))
    except ValueError as exc:
        if isinstance(exc, AgentFileError):
            raise
        raise AgentFileError(f"{name}: not JSON") from None


def read_json(root: Path, *parts: str, max_bytes: int) -> Any:
    """JSON at root/parts..., or FileNotFoundError; AgentFileError if it is not sane."""
    *dirs, name = parts
    with open_dir(root, *dirs) as fd:
        return read_json_at(fd, name, max_bytes)
