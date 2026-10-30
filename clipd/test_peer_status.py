"""Local status publication with synthetic peers and private temporary state."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import clipd
from private_files import write_private


class Peers:
    def peer_status(self):
        return [{'hostname': 'synthetic-peer', 'direction': 'out'}]

    def discovered_status(self):
        return []

    def is_available(self):
        return True

    def is_enabled(self):
        return True


class PeerStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='clipbar-status-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        context = patch.object(clipd, 'CONFIG_DIR', self.config)
        context.start()
        self.addCleanup(context.stop)
        self.status = self.config / 'peer-status.json'

    def test_atomic_private_status_and_replacement(self):
        clipd.write_peer_status(Peers(), Peers())
        self.assertEqual(self.status.stat().st_mode & 0o777, 0o600)
        snapshot = json.loads(self.status.read_text())
        self.assertEqual(snapshot['lan'][0]['hostname'], 'synthetic-peer')
        self.assertEqual(set(snapshot), {'self', 'lan', 'tailscale', 'tailscale_discovered',
                                        'tailscale_available', 'tailscale_enabled'})
        self.status.chmod(0o644)
        clipd.write_peer_status(Peers(), Peers())
        self.assertEqual(self.status.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.config.iterdir()), [self.status])

    def test_symlink_target_is_not_overwritten(self):
        outside = self.root / 'outside.txt'
        outside.write_text('synthetic sentinel')
        self.status.symlink_to(outside)
        clipd.write_peer_status(Peers(), Peers())
        self.assertEqual(outside.read_text(), 'synthetic sentinel')
        self.assertFalse(self.status.is_symlink())
        self.status.unlink()
        self.config.rmdir()
        self.config.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            clipd.write_peer_status(Peers(), Peers())
        self.assertFalse((self.root / 'peer-status.json').exists())

    async def test_publisher_removes_status_on_shutdown(self):
        task = asyncio.create_task(clipd.publish_peer_status(Peers(), Peers()))
        for _ in range(20):
            if self.status.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.status.exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.status.exists())

    def test_failed_atomic_replace_preserves_previous_status_and_cleans_temp(self):
        self.status.write_bytes(b'previous')
        with patch('private_files.os.replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                write_private(self.status, b'new')
        self.assertEqual(self.status.read_bytes(), b'previous')
        self.assertEqual(list(self.config.iterdir()), [self.status])


if __name__ == '__main__':
    unittest.main()
