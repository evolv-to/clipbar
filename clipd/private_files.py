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
