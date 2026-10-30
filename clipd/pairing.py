"""Explicit certificate pairing for TLS 1.3. No discovery-based trust."""
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import tempfile

from private_files import private_directory, read_private, write_private


def fingerprint(pem: str) -> str:
    if len(pem) > 16384 or pem.count('-----BEGIN CERTIFICATE-----') != 1:
        raise ValueError('expected one public certificate')
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem.strip())).hexdigest()


def normalize_fingerprint(value: str) -> str:
    value = value.replace(':', '').lower()
    if re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('expected a SHA-256 certificate fingerprint')
    return value


class Trust:
    def __init__(self, directory: Path):
        self.directory = directory
        private_directory(directory)
        self.cert = directory / 'identity.crt'
        self.key = directory / 'identity.key'
        self.path = directory / 'trust.json'
        self.ensure_identity()
        self.identity = fingerprint(read_private(self.cert).decode())
        self.peers = {}
        self.revision = b''
        self.reload()

    def ensure_identity(self):
        if self.cert.exists() or self.key.exists():
            # A partial identity is an error, never an implicit key rotation.
            read_private(self.cert)
            read_private(self.key)
            return
        with tempfile.TemporaryDirectory(prefix='.identity-', dir=self.directory) as folder:
            root = Path(folder)
            result = subprocess.run([
                'openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                '-nodes', '-days', '3650', '-subj', '/CN=ClipBar Device',
                '-addext', 'basicConstraints=critical,CA:TRUE',
                '-addext', 'keyUsage=critical,digitalSignature,keyCertSign',
                '-addext', 'extendedKeyUsage=serverAuth,clientAuth',
                '-addext', 'subjectKeyIdentifier=hash',
                '-addext', 'authorityKeyIdentifier=keyid:always',
                '-keyout', str(root / 'key'), '-out', str(root / 'cert'),
            ], capture_output=True, timeout=30)
            if result.returncode:
                raise ValueError('could not create local TLS identity')
            write_private(self.key, (root / 'key').read_bytes())
            write_private(self.cert, (root / 'cert').read_bytes())

    def reload(self) -> bool:
        try:
            data = read_private(self.path) if self.path.exists() else b'{}'
            peers = json.loads(data)
            if not isinstance(peers, dict) or len(peers) > 32:
                raise ValueError('invalid trust store')
            for key, peer in peers.items():
                if normalize_fingerprint(key) != key or fingerprint(peer['certificate']) != key:
                    raise ValueError('certificate does not match its approved fingerprint')
                if not isinstance(peer['name'], str) or not 1 <= len(peer['name']) <= 80 or any(ord(c) < 32 for c in peer['name']):
                    raise ValueError('invalid peer label')
        except Exception:
            # Malformed, unreadable, or insecure trust state grants no access.
            self.peers = {}
            self.revision = b''
            raise ValueError('trust store is invalid; sync is disabled') from None
        changed = data != self.revision
        self.peers, self.revision = peers, data
        return changed

    def pair(self, certificate: str, expected: str, name: str):
        expected = normalize_fingerprint(expected)
        if fingerprint(certificate) != expected or expected == self.identity:
            raise ValueError('certificate fingerprint mismatch or self-pairing')
        if not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
            raise ValueError('invalid peer label')
        # Parse/validate the certificate using the TLS library before persisting.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=certificate)
        self.reload()
        if len(self.peers) >= 32 and expected not in self.peers:
            raise ValueError('peer limit reached')
        self.peers[expected] = {'name': name, 'certificate': certificate}
        write_private(self.path, json.dumps(self.peers, indent=2).encode())
        self.reload()

    def unpair(self, identity: str):
        self.reload()
        self.peers.pop(normalize_fingerprint(identity), None)
        write_private(self.path, json.dumps(self.peers, indent=2).encode())
        self.reload()

    def context(self, server: bool) -> ssl.SSLContext:
        self.reload()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
        if not server:
            # A paired certificate is the identity, not its discovery address.
            context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(self.cert, self.key)
        if self.peers:
            context.load_verify_locations(cadata='\n'.join(p['certificate'] for p in self.peers.values()))
        return context

    def authorize(self, writer) -> tuple[str, str]:
        self.reload()
        tls = writer.get_extra_info('ssl_object')
        if tls is None or tls.version() != 'TLSv1.3':
            raise ValueError('encrypted paired connection required')
        identity = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
        # Exact leaf pin: even a certificate issued by a paired key is not a
        # separately approved device. Revocation is checked for every payload.
        if identity not in self.peers or identity == self.identity:
            raise ValueError('unapproved peer')
        return identity, self.peers[identity]['name']
