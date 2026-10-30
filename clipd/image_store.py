"""Owned PNG storage. Untrusted paths never select a file to read or delete."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import struct

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


def image_name(identifier: str) -> str:
    if not isinstance(identifier, str) or re.fullmatch(r'[0-9a-f]{64}', identifier) is None:
        raise ValueError('invalid image identifier')
    return identifier + '.png'


def owned_path(directory: Path, identifier: str, stored_path: str | None) -> Path:
    path = directory / image_name(identifier)
    if not isinstance(stored_path, str) or stored_path != str(path):
        raise ValueError('unowned image path')
    return path


def validate_png(raw: bytes) -> tuple[int, int]:
    if not 33 <= len(raw) <= MAX_IMAGE_BYTES or raw[:16] != b'\x89PNG\r\n\x1a\n\0\0\0\rIHDR':
        raise ValueError('invalid PNG')
    width, height = struct.unpack('>II', raw[16:24])
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        raise ValueError('image dimensions exceed the limit')
    from AppKit import NSBitmapImageRep, NSData
    data = NSData.dataWithBytes_length_(raw, len(raw))
    try:
        rep = NSBitmapImageRep.imageRepWithData_(data)
    except Exception:
        raise ValueError('invalid PNG pixels') from None
    if rep is None or rep.pixelsWide() != width or rep.pixelsHigh() != height:
        raise ValueError('invalid PNG pixels')
    return width, height


@contextmanager
def store_fd(directory: Path):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError('image store is not owned by this user')
        yield fd
    finally:
        os.close(fd)


def read_image(directory: Path, identifier: str, stored_path: str | None) -> bytes:
    owned_path(directory, identifier, stored_path)
    with store_fd(directory) as parent:
        fd = os.open(image_name(identifier), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                     dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_size > MAX_IMAGE_BYTES):
                raise ValueError('unowned or oversized image')
            raw = stream.read(MAX_IMAGE_BYTES + 1)
    if hashlib.sha256(raw).hexdigest() != identifier:
        raise ValueError('image content does not match identifier')
    validate_png(raw)
    return raw


def write_image(directory: Path, identifier: str, raw: bytes) -> Path:
    name = image_name(identifier)
    if hashlib.sha256(raw).hexdigest() != identifier:
        raise ValueError('image content does not match identifier')
    validate_png(raw)
    path = directory / name
    with store_fd(directory) as parent:
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
        except FileExistsError:
            read_image(directory, identifier, str(path))
            return path
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
        except BaseException:
            os.unlink(name, dir_fd=parent)
            raise
    return path


def delete_image(directory: Path, identifier: str, stored_path: str | None) -> None:
    owned_path(directory, identifier, stored_path)
    with store_fd(directory) as parent:
        name = image_name(identifier)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('unowned image')
        # unlink is relative to the opened store and never follows a symlink.
        os.unlink(name, dir_fd=parent)
