"""Capture, permission, cloud consent and purge tests use only synthetic state."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import clipd
from private_files import state_lock
from test_image_store import png
from image_store import write_image


class CapturePolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='clipbar-policy-'); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); self.config=self.root/'config'; self.cloud=self.root/'cloud'
        for key,value in {'CONFIG_DIR':self.config,'DB_PATH':self.config/'history.db',
                          'IMAGES_DIR':self.config/'images','ICLOUD_DIR':self.cloud,
                          'ICLOUD_ON_FLAG':self.config/'.icloud_on', 'ICLOUD_OFF_FLAG':self.config/'.icloud_off',
                          'PAUSE_FLAG':self.config/'.paused'}.items():
            context=patch.object(clipd,key,value); context.start(); self.addCleanup(context.stop)
        old=os.umask(0o022); self.addCleanup(os.umask,old)
        clipd.setup_dirs()
        self.conn=clipd.setup_db(); self.addCleanup(self.conn.close)

    async def test_all_excluded_markers_skip_payload_storage_logging_and_sinks(self):
        received=[]
        async def sink(item): received.append(item)
        watcher=clipd.ClipboardWatcher(self.conn,sink)
        class Pasteboard:
            def types(self): return ['public.utf8-plain-text',marker]
            def stringForType_(self,kind): raise AssertionError('excluded payload was accessed')
            def dataForType_(self,kind): raise AssertionError('excluded image was accessed')
        for marker in clipd.EXCLUDED_PASTEBOARD_TYPES:
            with self.assertNoLogs('clipd'):
                await watcher._handle(Pasteboard())
        self.assertEqual(received,[])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM items').fetchone()[0],0)
        self.assertEqual(list(clipd.IMAGES_DIR.iterdir()),[])
        self.assertFalse(self.cloud.exists())

    async def test_pause_skips_payload_and_resume_captures_new_item(self):
        received=[]
        async def sink(item): received.append(item)
        watcher=clipd.ClipboardWatcher(self.conn,sink)
        class Pasteboard:
            def types(self): return ['public.utf8-plain-text']
            def stringForType_(self,kind): return 'synthetic permitted content'
        clipd.PAUSE_FLAG.touch()
        await watcher._handle(Pasteboard())
        self.assertEqual(received,[])
        clipd.PAUSE_FLAG.unlink()
        with patch.object(clipd,'play_capture_sound'):
            await watcher._handle(Pasteboard())
        self.assertEqual(received[0]['content'],'synthetic permitted content')

    async def test_cloud_defaults_off_then_requires_explicit_opt_in(self):
        item={'type':'text','content':'synthetic cloud content'}
        sync=clipd.ICloudSync(self.conn)
        self.assertFalse(clipd._icloud_enabled())
        await sync.write(item)
        self.assertFalse(self.cloud.exists())
        clipd.ICLOUD_ON_FLAG.touch(); clipd.setup_dirs()
        self.assertTrue(clipd._icloud_enabled())
        await sync.write(item)
        snapshot=next(self.cloud.glob('*-latest.json'))
        self.assertEqual(snapshot.stat().st_mode & 0o777,0o600)
        clipd.ICLOUD_OFF_FLAG.touch()
        self.assertFalse(clipd._icloud_enabled())
        await sync.write(dict(item,content='synthetic change'))
        self.assertEqual(json.loads(snapshot.read_text())['content'],item['content'])

    def test_permissions_tightened_and_database_symlink_rejected(self):
        self.config.chmod(0o755); clipd.IMAGES_DIR.chmod(0o755); clipd.DB_PATH.chmod(0o644)
        raw=png(); identifier=hashlib.sha256(raw).hexdigest()
        image=write_image(clipd.IMAGES_DIR,identifier,raw); image.chmod(0o644)
        clipd.setup_dirs(); other=clipd.setup_db(); other.close()
        for path in (self.config,clipd.IMAGES_DIR): self.assertEqual(path.stat().st_mode & 0o777,0o700)
        for path in (clipd.DB_PATH,image): self.assertEqual(path.stat().st_mode & 0o777,0o600)
        outside=self.root/'sentinel'; outside.write_text('synthetic sentinel')
        bad=self.config/'bad.db'; bad.symlink_to(outside)
        with patch.object(clipd,'DB_PATH',bad):
            with self.assertRaises(OSError): clipd.setup_db()
        self.assertEqual(outside.read_text(),'synthetic sentinel')

    def test_purge_is_explicit_scoped_and_preserves_identity_and_outside_files(self):
        text='synthetic history'
        clipd.insert_item(self.conn,'text',text,None,'synthetic',hashlib.sha256(text.encode()).hexdigest())
        image=write_image(clipd.IMAGES_DIR,hashlib.sha256(png()).hexdigest(),png())
        key=self.config/'identity.key'; key.write_text('synthetic identity sentinel')
        outside=self.root/'outside'; outside.write_text('synthetic outside sentinel')
        link=clipd.IMAGES_DIR/('f'*64+'.png'); link.symlink_to(outside)
        self.cloud.mkdir(); cloud=self.cloud/'test-latest.json'; cloud.write_text('{}')
        other=self.cloud/'unrelated.txt'; other.write_text('synthetic unrelated')
        clipd.purge_history(True,False)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM items').fetchone()[0],0)
        self.assertFalse(image.exists()); self.assertTrue(cloud.exists()); self.assertTrue(link.is_symlink())
        self.assertEqual(key.read_text(),'synthetic identity sentinel')
        self.assertEqual(outside.read_text(),'synthetic outside sentinel')
        clipd.purge_history(False,True)
        self.assertFalse(cloud.exists()); self.assertTrue(other.exists())

    def test_runtime_lock_blocks_purge_and_duplicate_daemon(self):
        with state_lock(self.config,'runtime.lock',shared=True):
            with self.assertRaises(ValueError): clipd.purge_history(True,False)
        with state_lock(self.config,'daemon.lock'):
            with self.assertRaises(ValueError):
                with state_lock(self.config,'daemon.lock'): pass

    def test_cli_refuses_unconfirmed_or_unscoped_purge(self):
        env=dict(os.environ,CLIPBAR_CONFIG_DIR=str(self.config))
        for args in (['purge','--local'],['purge','--confirm']):
            result=subprocess.run([sys.executable,str(Path(clipd.__file__)),*args],env=env,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn(b'Traceback',result.stderr)


if __name__=='__main__': unittest.main()
