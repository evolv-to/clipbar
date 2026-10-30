"""macOS integration test: real builds, disposable keys, no login-key mutation."""
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin', 'macOS signing integration')
class SigningTests(unittest.TestCase):
    def run_command(self, args, *, env=None, timeout=90):
        process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail('Disposable signing operation timed out; process group stopped')
        self.assertEqual(process.returncode, 0, stderr.decode())
        return stdout

    def test_build_preserves_unrelated_key_acl_and_reuses_identity(self):
        with tempfile.TemporaryDirectory(prefix='clipbar-signing-test-') as folder:
            root = Path(folder)
            keychain = str(root / 'test.keychain-db')
            original = shlex.split(self.run_command(['security', 'list-keychains', '-d', 'user']).decode())
            self.run_command(['security', 'create-keychain', '-p', '', keychain])
            try:
                self.run_command(['security', 'unlock-keychain', '-p', '', keychain])
                self.run_command(['security', 'list-keychains', '-d', 'user', '-s', *original, keychain])
                config = root / 'sentinel.conf'
                config.write_text('[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3\n'
                                  '[dn]\nCN=Synthetic Unrelated Signing Key\n[v3]\n'
                                  'basicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\n'
                                  'extendedKeyUsage=critical,codeSigning\n')
                self.run_command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                                  '-days', '1', '-keyout', str(root / 'key.pem'), '-out',
                                  str(root / 'cert.pem'), '-config', str(config)])
                # This fixed password protects only a disposable synthetic P12.
                self.run_command(['openssl', 'pkcs12', '-export', '-inkey', str(root / 'key.pem'),
                                  '-in', str(root / 'cert.pem'), '-out', str(root / 'sentinel.p12'),
                                  '-name', 'Synthetic Unrelated Signing Key', '-passout',
                                  'pass:synthetic-test-only', '-macalg', 'sha1', '-keypbe',
                                  'PBE-SHA1-3DES', '-certpbe', 'PBE-SHA1-3DES'])
                self.run_command(['security', 'import', str(root / 'sentinel.p12'), '-k', keychain,
                                  '-P', 'synthetic-test-only', '-T', '/usr/bin/true'])

                def sentinel_acl():
                    dump = self.run_command(['security', 'dump-keychain', '-a', keychain])
                    entries = [part for part in dump.split(b'keychain:')
                               if b'Synthetic Unrelated Signing Key' in part]
                    self.assertTrue(entries)
                    return entries

                before = sentinel_acl()
                app = root / 'ClipBar'
                app.mkdir()
                for filename in ('build.sh', 'Info.plist'):
                    shutil.copyfile(SOURCE / 'ClipBar' / filename, app / filename)
                shutil.copytree(SOURCE / 'ClipBar/Sources', app / 'Sources')
                env = dict(os.environ, CLIPBAR_SIGNING_KEYCHAIN=keychain,
                           CLANG_MODULE_CACHE_PATH=str(root / 'module-cache'))
                identities = []
                for _ in range(2):
                    self.run_command(['bash', str(app / 'build.sh')], env=env, timeout=120)
                    self.run_command(['codesign', '--verify', '--strict',
                                      str(app / 'ClipBar.app/Contents/MacOS/ClipBar')])
                    identities.append(self.run_command(['security', 'find-identity', '-p', 'codesigning', keychain]))
                    self.assertEqual(sentinel_acl(), before, 'unrelated key attributes/ACL changed')
                self.assertEqual(identities[0], identities[1], 'second build created a different identity')
            finally:
                self.run_command(['security', 'delete-keychain', keychain])
                # Preserve any unrelated search-list updates made during the test.
                current = shlex.split(self.run_command(['security', 'list-keychains', '-d', 'user']).decode())
                remaining = [path for path in current if path != keychain]
                if remaining != current:
                    self.run_command(['security', 'list-keychains', '-d', 'user', '-s', *remaining])
                self.assertEqual(remaining, original, 'keychain search list changed during test')


if __name__ == '__main__':
    unittest.main()
