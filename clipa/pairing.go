package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

type trustedPeer struct {
	Name        string `json:"name"`
	Certificate string `json:"certificate"`
}

type trustStore struct {
	directory   string
	identity    string
	certificate tls.Certificate
}

func privateRead(path string) ([]byte, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	owner, ok := info.Sys().(*syscall.Stat_t)
	if !ok || owner.Uid != uint32(os.Getuid()) || owner.Nlink != 1 || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() > 1024*1024 {
		return nil, errors.New("state file is not private and owned")
	}
	data := make([]byte, info.Size())
	_, err = io.ReadFull(file, data)
	return data, err
}

// Atomic replace changes a directory entry, never the target of a symlink.
func privateWrite(path string, data []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".clipbar-")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

func parseCertificate(data []byte) (*x509.Certificate, error) {
	if len(data) > 16384 {
		return nil, errors.New("certificate too large")
	}
	block, rest := pem.Decode(data)
	if block == nil || block.Type != "CERTIFICATE" || strings.TrimSpace(string(rest)) != "" {
		return nil, errors.New("expected one public certificate")
	}
	return x509.ParseCertificate(block.Bytes)
}

func normalizeFingerprint(value string) (string, error) {
	value = strings.ToLower(strings.ReplaceAll(value, ":", ""))
	decoded, err := hex.DecodeString(value)
	if err != nil || len(decoded) != 32 {
		return "", errors.New("expected SHA-256 certificate fingerprint")
	}
	return value, nil
}

func newTrust(directory string) (*trustStore, error) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("invalid state directory")
	}
	owner, ok := info.Sys().(*syscall.Stat_t)
	if !ok || owner.Uid != uint32(os.Getuid()) {
		return nil, errors.New("state directory is not owned")
	}
	if err := os.Chmod(directory, 0700); err != nil {
		return nil, err
	}
	certPath, keyPath := filepath.Join(directory, "identity.crt"), filepath.Join(directory, "identity.key")
	_, certErr := os.Lstat(certPath)
	_, keyErr := os.Lstat(keyPath)
	if os.IsNotExist(certErr) && os.IsNotExist(keyErr) {
		key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			return nil, err
		}
		serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
		if err != nil {
			return nil, err
		}
		public, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
		if err != nil {
			return nil, err
		}
		id := sha256.Sum256(public)
		cert := &x509.Certificate{SerialNumber: serial, Subject: pkix.Name{CommonName: "ClipBar Device"},
			NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().AddDate(10, 0, 0),
			KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
			BasicConstraintsValid: true, IsCA: true, SubjectKeyId: id[:20], AuthorityKeyId: id[:20]}
		der, err := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
		if err != nil {
			return nil, err
		}
		private, err := x509.MarshalPKCS8PrivateKey(key)
		if err != nil {
			return nil, err
		}
		if err = privateWrite(keyPath, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: private})); err != nil {
			return nil, err
		}
		if err = privateWrite(certPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})); err != nil {
			return nil, err
		}
	}
	certPEM, err := privateRead(certPath)
	if err != nil {
		return nil, err
	}
	keyPEM, err := privateRead(keyPath)
	if err != nil {
		return nil, err
	}
	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, err
	}
	store := &trustStore{directory: directory, identity: sha256Hex(pair.Certificate[0]), certificate: pair}
	_, err = store.peers()
	return store, err
}

func (t *trustStore) peers() (map[string]trustedPeer, error) {
	result := make(map[string]trustedPeer)
	raw, err := privateRead(filepath.Join(t.directory, "trust.json"))
	if os.IsNotExist(err) {
		return result, nil
	}
	if err != nil {
		return nil, err
	}
	if err = json.Unmarshal(raw, &result); err != nil || len(result) > 32 {
		return nil, errors.New("invalid trust store")
	}
	for fingerprint, peer := range result {
		cert, err := parseCertificate([]byte(peer.Certificate))
		if err != nil || sha256Hex(cert.Raw) != fingerprint || !validPeerName(peer.Name) {
			return nil, errors.New("invalid paired certificate")
		}
	}
	return result, nil
}

func validPeerName(name string) bool {
	if len(name) == 0 || len(name) > 80 {
		return false
	}
	for _, c := range name {
		if c < 32 {
			return false
		}
	}
	return true
}

func (t *trustStore) pair(data []byte, expected, name string) error {
	expected, err := normalizeFingerprint(expected)
	if err != nil {
		return err
	}
	cert, err := parseCertificate(data)
	if err != nil {
		return err
	}
	if sha256Hex(cert.Raw) != expected || expected == t.identity || !validPeerName(name) {
		return errors.New("fingerprint mismatch, self-pairing, or invalid label")
	}
	peers, err := t.peers()
	if err != nil {
		return err
	}
	if _, exists := peers[expected]; !exists && len(peers) >= 32 {
		return errors.New("peer limit reached")
	}
	peers[expected] = trustedPeer{Name: name, Certificate: string(data)}
	raw, err := json.MarshalIndent(peers, "", "  ")
	if err != nil {
		return err
	}
	return privateWrite(filepath.Join(t.directory, "trust.json"), raw)
}

func (t *trustStore) unpair(identity string) error {
	identity, err := normalizeFingerprint(identity)
	if err != nil {
		return err
	}
	peers, err := t.peers()
	if err != nil {
		return err
	}
	delete(peers, identity)
	raw, err := json.MarshalIndent(peers, "", "  ")
	if err != nil {
		return err
	}
	return privateWrite(filepath.Join(t.directory, "trust.json"), raw)
}

func (t *trustStore) authorize(c net.Conn) (string, string, error) {
	tlsConn, ok := c.(*tls.Conn)
	if !ok {
		return "", "", errors.New("TLS required")
	}
	state := tlsConn.ConnectionState()
	if !state.HandshakeComplete || state.Version != tls.VersionTLS13 || len(state.PeerCertificates) == 0 {
		return "", "", errors.New("paired TLS required")
	}
	identity := sha256Hex(state.PeerCertificates[0].Raw)
	peers, err := t.peers()
	if err != nil {
		return "", "", err
	}
	peer, exists := peers[identity]
	if !exists || identity == t.identity {
		return "", "", errors.New("unapproved peer")
	}
	return identity, peer.Name, nil
}

func (t *trustStore) config(server bool) (*tls.Config, error) {
	peers, err := t.peers()
	if err != nil {
		return nil, err
	}
	roots := x509.NewCertPool()
	for _, peer := range peers {
		roots.AppendCertsFromPEM([]byte(peer.Certificate))
	}
	config := &tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{t.certificate}, SessionTicketsDisabled: true}
	if server {
		config.ClientAuth = tls.RequireAndVerifyClientCert
		config.ClientCAs = roots
	} else {
		// Addresses are discovered and are not identities. Perform full chain/time/
		// usage validation below, followed by an exact paired leaf fingerprint.
		config.InsecureSkipVerify = true
	}
	config.VerifyConnection = func(state tls.ConnectionState) error {
		if len(state.PeerCertificates) == 0 {
			return errors.New("peer certificate required")
		}
		identity := sha256Hex(state.PeerCertificates[0].Raw)
		current, err := t.peers()
		if err != nil {
			return err
		}
		approved, exists := current[identity]
		if !exists || identity == t.identity {
			return errors.New("unapproved peer")
		}
		exactRoots := x509.NewCertPool()
		exactRoots.AppendCertsFromPEM([]byte(approved.Certificate))
		usage := x509.ExtKeyUsageServerAuth
		if server {
			usage = x509.ExtKeyUsageClientAuth
		}
		_, err = state.PeerCertificates[0].Verify(x509.VerifyOptions{Roots: exactRoots, KeyUsages: []x509.ExtKeyUsage{usage}})
		return err
	}
	return config, nil
}

func pairingError(err error) {
	fmt.Fprintln(os.Stderr, "Pairing failed; check certificate, fingerprint, and private state permissions.")
	os.Exit(1)
}
