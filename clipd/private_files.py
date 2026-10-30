"""Atomic owner-only local state; never follow a final directory/file symlink."""
import os
from pathlib import Path
import secrets


def write_private(path: Path, data: bytes) -> None:
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = '.clipbar-' + secrets.token_hex(16)
    try:
        if os.fstat(parent).st_uid != os.getuid():
            raise ValueError('state directory is not owned by this user')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError('state directory is not owned by this user')
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def read_private(path: Path, limit: int = 1024 * 1024) -> bytes:
    import stat
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(parent).st_uid != os.getuid():
            raise ValueError('state directory is not owned by this user')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit):
                raise ValueError('state file is not a private owned regular file')
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError('state file is too large')
            return data
    finally:
        os.close(parent)
