"""Exercise clipboard flows with synthetic data and an isolated history DB."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clipd


class ClipboardLogPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path_patch = patch.object(clipd, "DB_PATH", self.directory / "history.db")
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.conn = clipd.setup_db()
        self.addCleanup(self.conn.close)
        self.content = "synthetic-private-clipboard-content"
        self.preview = "synthetic-private-clipboard-preview"
        self.item = {
            "type": "text", "content": self.content, "preview": self.preview,
            "content_hash": hashlib.sha256(self.content.encode()).hexdigest(),
            "source_mac": "test-peer",
        }

    def assert_private_log(self, captured):
        output = "\n".join(captured.output)
        self.assertNotIn(self.content, output)
        self.assertNotIn(self.preview, output)
        row = self.conn.execute("SELECT content FROM items").fetchone()
        self.assertEqual(row["content"], self.content)

    def test_capture_stores_and_forwards_text_without_logging_it(self):
        class Pasteboard:
            def stringForType_(inner, _type):
                return self.content

        received = []

        async def on_new(item):
            received.append(item)

        watcher = clipd.ClipboardWatcher(self.conn, on_new)
        with patch.object(clipd, "play_capture_sound"), self.assertLogs("clipd", "INFO") as logs:
            asyncio.run(watcher._handle(Pasteboard()))
        self.assert_private_log(logs)
        self.assertEqual(received[0]["content"], self.content)

    def test_peer_ingest_preserves_content_without_logging_preview(self):
        with self.assertLogs("clipd", "INFO") as logs:
            self.assertTrue(clipd.ingest_item(self.conn, self.item, "test-peer"))
        self.assert_private_log(logs)

    def test_icloud_ingest_preserves_content_without_logging_preview(self):
        snapshot = self.directory / "snapshot.json"
        snapshot.write_text(json.dumps(self.item))
        with self.assertLogs("clipd", "INFO") as logs:
            asyncio.run(clipd.ICloudSync(self.conn)._ingest(snapshot))
        self.assert_private_log(logs)


if __name__ == "__main__":
    unittest.main()
