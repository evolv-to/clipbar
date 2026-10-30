"""Run the real Go executable against Python TLS using synthetic Termux shims."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'clipd'))
import clipd
from pairing import Trust


class CrossClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_go_mutual_tls_bidirectional_payloads(self):
        with tempfile.TemporaryDirectory(prefix='clipbar-interop-') as folder:
            root=Path(folder); binary=root/'clipa'
            subprocess.run(['go','build','-o',str(binary),'.'],cwd=ROOT/'clipa',check=True,capture_output=True)
            mac=Trust(root/'mac'); android=root/'android'
            result=subprocess.run([str(binary),'--config',str(android),'--identity'],check=True,capture_output=True,text=True)
            peer_id=result.stdout.strip()
            mac.pair((android/'identity.crt').read_text(),peer_id,'synthetic-android')
            subprocess.run([str(binary),'--config',str(android),'--pair',str(mac.cert),
                            '--fingerprint',mac.identity,'--name','synthetic-mac'],check=True,capture_output=True)
            (android/'peers.json').write_text('[]')
            shims=root/'bin'; shims.mkdir()
            input_file=root/'clipboard-input'
            input_file.write_text('synthetic-from-android')
            (shims/'termux-clipboard-get').write_text('#!/bin/sh\ncat "$CLIPBAR_TEST_INPUT"\n')
            (shims/'termux-clipboard-set').write_text('#!/bin/sh\ncat > "$CLIPBAR_TEST_OUTPUT"\n')
            for path in shims.iterdir(): path.chmod(0o700)
            output=root/'received'; log=root/'process.log'
            env=dict(os.environ,PATH=str(shims)+os.pathsep+os.environ['PATH'],CLIPBAR_TEST_OUTPUT=str(output),
                     CLIPBAR_TEST_INPUT=str(input_file))
            with log.open('wb') as stream:
                process=subprocess.Popen([str(binary),'--config',str(android),'--hostname','synthetic-android',
                                          '--listen','127.0.0.1:0'],env=env,stdout=stream,stderr=stream)
                writer=None
                try:
                    port=None
                    for _ in range(200):
                        match=re.search(r'listening on 127\.0\.0\.1:(\d+)',log.read_text())
                        if match: port=int(match[1]); break
                        if process.poll() is not None: self.fail('Go peer exited unexpectedly')
                        await asyncio.sleep(0.02)
                    self.assertIsNotNone(port,'Go listener did not start')
                    reader,writer=await asyncio.open_connection('127.0.0.1',port,ssl=mac.context(False),server_hostname='')
                    self.assertEqual(mac.authorize(writer)[0],peer_id)
                    writer.write(clipd._frame(clipd._hello('stream'))); await writer.drain()
                    clipd._check_hello(await clipd._read_frame(reader,5),'stream')
                    text='synthetic-from-mac'
                    writer.write(clipd._frame({'type':'text','content':text,'content_hash':hashlib.sha256(text.encode()).hexdigest()}))
                    await writer.drain()
                    for _ in range(100):
                        if output.exists() and output.read_text()==text: break
                        await asyncio.sleep(0.02)
                    self.assertEqual(output.read_text(),text)
                    received=await clipd._read_frame(reader,5)
                    self.assertEqual(received['content'],'synthetic-from-android')
                    self.assertEqual(received['content_hash'],hashlib.sha256(received['content'].encode()).hexdigest())
                    self.assertNotIn(text,log.read_text())
                    self.assertNotIn(received['content'],log.read_text())
                    # Real CLI revocation closes the existing Go connection.
                    subprocess.run([str(binary),'--config',str(android),'--unpair',mac.identity],check=True,capture_output=True)
                    self.assertEqual(await asyncio.wait_for(reader.read(),3),b'')
                    # Reverse the TLS roles and exercise the actual Go dial loop.
                    subprocess.run([str(binary),'--config',str(android),'--pair',str(mac.cert),
                                    '--fingerprint',mac.identity,'--name','synthetic-mac'],check=True,capture_output=True)
                    with patch.object(clipd,'DB_PATH',root/'history.db'), patch.object(clipd,'TAILSCALE_OFF_FLAG',root/'off'):
                        conn=clipd.setup_db()
                        lan=clipd.LANSync(conn,mac)
                        ts=clipd.TailscaleSync(conn,lambda:set(),mac)
                        lan.attach_tailscale(ts)
                        server=await asyncio.start_server(lan._handle_peer,'127.0.0.1',0,ssl=mac.context(True))
                        try:
                            address='127.0.0.1:'+str(server.sockets[0].getsockname()[1])
                            (android/'peers.json').write_text(json.dumps([{'hostname':'synthetic-mac','address':address}]))
                            for _ in range(700):
                                if ts._connections: break
                                await asyncio.sleep(0.02)
                            self.assertTrue(ts._connections,'Go dial loop did not establish the paired stream')
                            text='synthetic-mac-reverse-connection'
                            await ts.broadcast({'type':'text','content':text,'content_hash':hashlib.sha256(text.encode()).hexdigest()})
                            for _ in range(100):
                                if output.read_text()==text: break
                                await asyncio.sleep(0.02)
                            self.assertEqual(output.read_text(),text)
                            input_file.write_text('synthetic-go-reconnected')
                            for _ in range(200):
                                row=conn.execute('SELECT content FROM items').fetchone()
                                if row: break
                                await asyncio.sleep(0.02)
                            self.assertEqual(row['content'],'synthetic-go-reconnected')
                        finally:
                            ts._close_all('test cleanup')
                            server.close(); await server.wait_closed()
                            await asyncio.sleep(0.02)
                            conn.close()
                finally:
                    if writer: writer.close()
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()


if __name__=='__main__': unittest.main()
