"""Real TLS connections with temporary identities and synthetic clipboard items."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import clipd
from pairing import Trust
from test_image_store import png


class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipbar-pairing-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a, self.b, self.c = [Trust(self.root / name) for name in ('a','b','c')]
        self.pair(self.a,self.b)
        self.pair(self.b,self.a)
        self.pair(self.c,self.b)  # B has NOT approved C.
        self.images = self.root / 'images'
        self.images.mkdir()
        for key,value in {'DB_PATH':self.root/'history.db','IMAGES_DIR':self.images,
                          'TAILSCALE_OFF_FLAG':self.root/'off'}.items():
            context=patch.object(clipd,key,value); context.start(); self.addCleanup(context.stop)
        self.conn=clipd.setup_db(); self.addCleanup(self.conn.close)
        self.lan=clipd.LANSync(self.conn,self.b)
        self.ts=clipd.TailscaleSync(self.conn,lambda:set(),self.b)
        self.lan.attach_tailscale(self.ts)
        self.server=await asyncio.start_server(self.lan._handle_peer,'127.0.0.1',0,
                                              ssl=self.b.context(True),ssl_handshake_timeout=1,
                                              ssl_shutdown_timeout=1)
        self.port=self.server.sockets[0].getsockname()[1]
        self.writers=[]
        self.item={'type':'text','content':'synthetic paired text', 'source_mac':'synthetic-peer',
                   'content_hash':hashlib.sha256(b'synthetic paired text').hexdigest()}

    def pair(self,a,b):
        a.pair(b.cert.read_text(),b.identity,'synthetic-peer')

    async def asyncTearDown(self):
        self.ts._close_all('test cleanup')
        for writer in self.writers:
            writer.close()
        await asyncio.sleep(0.02)
        self.server.close(); await self.server.wait_closed()

    async def connect(self,trust):
        reader,writer=await asyncio.open_connection('127.0.0.1',self.port,
            ssl=trust.context(False),server_hostname='',ssl_shutdown_timeout=1)
        self.writers.append(writer)
        return reader,writer

    async def wait_rows(self,count):
        for _ in range(100):
            actual=self.conn.execute('SELECT count(*) FROM items').fetchone()[0]
            if actual==count: return
            await asyncio.sleep(0.01)
        self.assertEqual(actual,count)

    async def test_approved_text_and_image_and_mutual_hello(self):
        for item in (self.item,{'type':'image','source_mac':'synthetic-peer',
                 'content_hash':hashlib.sha256(png()).hexdigest(),
                 'image_data':base64.b64encode(png()).decode()}):
            reader,writer=await self.connect(self.a)
            self.assertEqual(self.a.authorize(writer)[0],self.b.identity)
            writer.write(clipd._frame(clipd._hello('once')))
            await writer.drain()
            self.assertEqual(clipd._check_hello(await clipd._read_frame(reader)), 'once')
            writer.write(clipd._frame(item)); await writer.drain()
        await self.wait_rows(2)
        self.assertEqual(next(self.images.iterdir()).read_bytes(),png())

    async def test_unapproved_inbound_and_plaintext_rejected(self):
        try:
            reader,writer=await self.connect(self.c)
            writer.write(clipd._frame(clipd._hello('once'))+clipd._frame(self.item))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
        except (ConnectionError,OSError):
            pass
        reader,writer=await asyncio.open_connection('127.0.0.1',self.port)
        self.writers.append(writer)
        writer.write(clipd._frame(self.item)); await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM items').fetchone()[0],0)

    async def test_legacy_unknown_versions_and_item_first_rejected(self):
        for first in (dict(clipd._hello('once'),version=1),dict(clipd._hello('once'),version=99),self.item):
            reader,writer=await self.connect(self.a)
            writer.write(clipd._frame(first)); await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM items').fetchone()[0],0)

    async def test_revocation_blocks_existing_connection_before_ingest_or_send(self):
        reader,writer=await self.connect(self.a)
        writer.write(clipd._frame(clipd._hello('stream'))); await writer.drain()
        await clipd._read_frame(reader)
        for _ in range(50):
            if self.ts._connections: break
            await asyncio.sleep(0.01)
        self.assertTrue(self.ts._connections)
        self.b.unpair(self.a.identity)
        await self.ts.broadcast(self.item)
        self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM items').fetchone()[0],0)

    async def test_unapproved_advertiser_receives_no_payload(self):
        received=[]
        async def attacker(reader,writer):
            received.append(await reader.read())
            writer.close()
        self.pair(self.c,self.a)
        evil=await asyncio.start_server(attacker,'127.0.0.1',0,ssl=self.c.context(True),ssl_handshake_timeout=1)
        port=evil.sockets[0].getsockname()[1]
        sender=clipd.LANSync(self.conn,self.a)
        # Attacker advertises an approved device's fingerprint and address.
        sender._peers['synthetic']=('synthetic','','127.0.0.1',port,self.b.identity)
        try:
            await sender.broadcast(self.item)
            self.assertEqual(sender.last_sent,set())
            self.assertFalse(any(received))
        finally:
            evil.close(); await evil.wait_closed()

    async def test_real_lan_broadcast_requires_matching_approved_fingerprint(self):
        sender=clipd.LANSync(self.conn,self.a)
        sender._peers['synthetic']=('synthetic','','127.0.0.1',self.port,self.b.identity)
        await sender.broadcast(self.item)
        await self.wait_rows(1)
        self.assertEqual(sender.last_sent,{self.b.identity})

    async def test_fingerprint_confirmation_and_private_permissions(self):
        with self.assertRaises(ValueError): self.a.pair(self.c.cert.read_text(),'0'*64,'synthetic')
        self.assertEqual(self.a.key.stat().st_mode & 0o777,0o600)
        self.a.path.chmod(0o644)
        with self.assertRaises(ValueError): self.a.context(False)
        self.assertEqual(self.a.peers,{})

    async def test_tailscale_toggle_preserves_authenticated_lan_stream(self):
        reader,writer=await self.connect(self.a)
        writer.write(clipd._frame(clipd._hello('stream'))); await writer.drain()
        await clipd._read_frame(reader)
        for _ in range(50):
            if self.ts._connections: break
            await asyncio.sleep(0.01)
        (self.root/'off').touch()
        self.ts.close_unapproved()
        await self.ts.broadcast(self.item)
        self.assertEqual((await clipd._read_frame(reader,2))['content'],self.item['content'])
        self.assertEqual(self.ts.peer_status()[0]['transport'],'lan')


if __name__=='__main__': unittest.main()
