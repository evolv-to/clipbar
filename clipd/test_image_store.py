"""Synthetic image/file-boundary regressions; no real history or pasteboard."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import clipd
from image_store import read_image, write_image


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0\xff')) + chunk(b'IEND', b''))


class ImageBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='clipbar-images-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.images = self.root / 'images'
        self.images.mkdir()
        self.cloud = self.root / 'cloud'
        self.cloud.mkdir()
        for key, value in {'DB_PATH': self.root / 'history.db', 'IMAGES_DIR': self.images,
                           'ICLOUD_DIR': self.cloud, 'ICLOUD_OFF_FLAG': self.root / 'off',
                           'ICLOUD_ON_FLAG': self.root / 'on', 'PAUSE_FLAG': self.root / 'paused'}.items():
            context = patch.object(clipd, key, value)
            context.start()
            self.addCleanup(context.stop)
        (self.root / 'on').touch()
        self.conn = clipd.setup_db()
        self.addCleanup(self.conn.close)
        self.raw = png()
        self.identifier = hashlib.sha256(self.raw).hexdigest()
        self.path = self.images / (self.identifier + '.png')
        self.outside = self.root / 'outside.png'
        self.outside.write_bytes(self.raw)
        self.item = {'type': 'image', 'content_hash': self.identifier,
                     'image_data': base64.b64encode(self.raw).decode(),
                     'image_path': str(self.outside), 'preview': 'untrusted preview', 'source_mac': 'synthetic-peer'}

    def assert_empty(self):
        self.assertEqual(self.conn.execute('SELECT count(*) FROM items').fetchone()[0], 0)
        self.assertEqual(self.outside.read_bytes(), self.raw)

    def test_valid_image_is_owned_and_sender_path_ignored(self):
        original = dict(self.item)
        self.assertTrue(clipd.ingest_item(self.conn, self.item, 'test'))
        self.assertEqual(self.item, original)
        row = self.conn.execute('SELECT * FROM items').fetchone()
        self.assertEqual(row['image_path'], str(self.path))
        self.assertEqual(read_image(self.images, self.identifier, row['image_path']), self.raw)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(row['preview'], '[image 1×1]')

    async def test_capture_preserves_valid_png_and_forwards_synthetic_bytes(self):
        received = []
        raw = self.raw

        class Pasteboard:
            def types(self):
                return []

            def stringForType_(self, kind):
                return None

            def dataForType_(self, kind):
                return raw if kind == 'public.png' else None

        async def on_new(item):
            received.append(item)

        with patch.object(clipd, 'play_capture_sound'):
            await clipd.ClipboardWatcher(self.conn, on_new)._handle(Pasteboard())
        self.assertEqual(read_image(self.images, self.identifier, str(self.path)), self.raw)
        self.assertEqual(base64.b64decode(received[0]['image_data']), self.raw)

    def test_bad_identifiers_and_payloads_cannot_write_or_insert(self):
        variants = [dict(self.item, content_hash=value) for value in
                    ('../outside', str(self.outside), '%2foutside', 'A' * 64, 'f' * 64, None)]
        variants += [dict(self.item, image_data=value) for value in ('!!!', '', None, base64.b64encode(b'not PNG').decode())]
        variants += [dict(self.item, type='unknown'), [], {'type': 'image'}]
        for item in variants:
            with self.subTest(item_type=type(item).__name__):
                self.assertFalse(clipd.ingest_item(self.conn, item, 'test'))
                self.assert_empty()
        self.assertEqual(list(self.images.iterdir()), [])

    def test_text_hash_type_and_path_validation(self):
        text = 'synthetic text'
        item = {'type': 'text', 'content': text, 'content_hash': hashlib.sha256(text.encode()).hexdigest(),
                'image_path': str(self.outside), 'source_mac': 'synthetic-peer'}
        self.assertTrue(clipd.ingest_item(self.conn, item, 'test'))
        self.assertIsNone(self.conn.execute('SELECT image_path FROM items').fetchone()[0])
        self.assertFalse(clipd.ingest_item(self.conn, dict(item, content='changed'), 'test'))
        self.assertFalse(clipd.ingest_item(self.conn, dict(item, content={}), 'test'))
        self.assertFalse(clipd.ingest_item(self.conn, dict(item, source_mac=['unexpected']), 'test'))

    def test_image_symlinks_and_hardlinks_rejected_for_read_write_prune(self):
        for link in ('symlink', 'hardlink'):
            with self.subTest(link=link):
                if link == 'symlink':
                    self.path.symlink_to(self.outside)
                else:
                    os.link(self.outside, self.path)
                with self.assertRaises((OSError, ValueError)):
                    read_image(self.images, self.identifier, str(self.path))
                self.assertFalse(clipd.ingest_item(self.conn, self.item, 'test'))
                with patch.object(clipd, 'MAX_ITEMS', 0):
                    clipd.insert_item(self.conn, 'image', None, str(self.path), 'synthetic', self.identifier)
                self.assertEqual(self.outside.read_bytes(), self.raw)
                self.assertTrue(self.path.exists())
                self.path.unlink()

    def test_store_directory_symlink_and_legacy_outside_path(self):
        self.images.rmdir()
        self.images.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(clipd.ingest_item(self.conn, self.item, 'test'))
        self.images.unlink()
        self.images.mkdir()
        clipd.insert_item(self.conn, 'image', None, str(self.outside), 'synthetic', self.identifier)
        with patch.object(clipd, 'MAX_ITEMS', 0):
            clipd.prune_items(self.conn)
        self.assert_empty()

    def test_legacy_path_quarantine_does_not_open_or_delete_target(self):
        clipd.insert_item(self.conn, 'image', None, str(self.outside), 'synthetic', self.identifier)
        migrated = clipd.setup_db()
        try:
            self.assertIsNone(migrated.execute('SELECT image_path FROM items').fetchone()[0])
            self.assertEqual(self.outside.read_bytes(), self.raw)
        finally:
            migrated.close()

    def test_valid_prune_and_corrupt_image(self):
        self.assertTrue(clipd.ingest_item(self.conn, self.item, 'test'))
        self.path.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            read_image(self.images, self.identifier, str(self.path))
        with patch.object(clipd, 'MAX_ITEMS', 0):
            clipd.prune_items(self.conn)
        self.assertFalse(self.path.exists())
        self.assert_empty()

    async def test_cloud_ingest_uses_same_validation_and_rejects_snapshot_symlink(self):
        snapshot = self.cloud / 'synthetic-latest.json'
        snapshot.write_text(json.dumps(dict(self.item, content_hash='../outside')))
        await clipd.ICloudSync(self.conn)._ingest(snapshot)
        self.assert_empty()
        snapshot.write_text(json.dumps(self.item))
        await clipd.ICloudSync(self.conn)._ingest(snapshot)
        self.assertEqual(read_image(self.images, self.identifier, str(self.path)), self.raw)
        snapshot.unlink()
        snapshot.symlink_to(self.outside)
        await clipd.ICloudSync(self.conn)._ingest(snapshot)
        self.assertEqual(self.outside.read_bytes(), self.raw)

    async def test_cloud_export_never_reads_sender_path_or_follows_destination_link(self):
        sync = clipd.ICloudSync(self.conn)
        await sync.write(self.item)
        self.assertEqual(list(self.cloud.iterdir()), [])
        self.assertTrue(clipd.ingest_item(self.conn, self.item, 'test'))
        row = dict(self.conn.execute('SELECT * FROM items').fetchone())
        target = self.cloud / f'{clipd.HOSTNAME}-latest.json'
        target.symlink_to(self.outside)
        await sync.write(row)
        self.assertEqual(self.outside.read_bytes(), self.raw)
        payload = json.loads(target.read_text())
        self.assertNotIn('image_path', payload)
        self.assertEqual(base64.b64decode(payload['image_data']), self.raw)

    def test_wire_frame_excludes_local_paths(self):
        payload = json.loads(clipd._frame(self.item)[4:])
        self.assertNotIn('image_path', payload)


if __name__ == '__main__':
    unittest.main()
