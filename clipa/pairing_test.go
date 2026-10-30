package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func testTrust(t *testing.T) *trustStore {
	t.Helper()
	trust, err := newTrust(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return trust
}
func approve(t *testing.T, a, b *trustStore) {
	t.Helper()
	raw, err := privateRead(filepath.Join(b.directory, "identity.crt"))
	if err != nil {
		t.Fatal(err)
	}
	if err = a.pair(raw, b.identity, "synthetic-peer"); err != nil {
		t.Fatal(err)
	}
}
func pairedConnection(t *testing.T, a, b *trustStore) (net.Conn, net.Conn) {
	t.Helper()
	clientConfig, err := a.config(false)
	if err != nil {
		t.Fatal(err)
	}
	serverConfig, err := b.config(true)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := tls.Listen("tcp", "127.0.0.1:0", serverConfig)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	accepted := make(chan net.Conn, 1)
	go func() {
		c, err := listener.Accept()
		if err == nil {
			c.SetDeadline(time.Now().Add(5 * time.Second))
			if c.(*tls.Conn).Handshake() == nil {
				accepted <- c
			} else {
				c.Close()
			}
		}
	}()
	client, err := tls.Dial("tcp", listener.Addr().String(), clientConfig)
	if err != nil {
		t.Fatal(err)
	}
	var server net.Conn
	select {
	case server = <-accepted:
	case <-time.After(5 * time.Second):
		t.Fatal("handshake timeout")
	}
	client.SetDeadline(time.Now().Add(5 * time.Second))
	t.Cleanup(func() { client.Close(); server.Close() })
	return client, server
}

func TestPairedTLSAndRevocation(t *testing.T) {
	a, b := testTrust(t), testTrust(t)
	approve(t, a, b)
	approve(t, b, a)
	client, _ := pairedConnection(t, a, b)
	if id, _, err := a.authorize(client); err != nil || id != b.identity {
		t.Fatalf("paired peer rejected: %v", err)
	}
	if err := a.unpair(b.identity); err != nil {
		t.Fatal(err)
	}
	if _, _, err := a.authorize(client); err == nil {
		t.Fatal("revoked connection authorized")
	}
}
func TestUnapprovedAndWrongFingerprintRejected(t *testing.T) {
	a, b := testTrust(t), testTrust(t)
	raw, _ := privateRead(filepath.Join(b.directory, "identity.crt"))
	if a.pair(raw, a.identity, "synthetic") == nil {
		t.Fatal("wrong fingerprint accepted")
	}
	approve(t, b, a)
	sc, _ := b.config(true)
	listener, err := tls.Listen("tcp", "127.0.0.1:0", sc)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	done := make(chan struct{})
	go func() {
		defer close(done)
		c, err := listener.Accept()
		if err == nil {
			defer c.Close()
			c.SetDeadline(time.Now().Add(time.Second))
			c.(*tls.Conn).Handshake()
		}
	}()
	cc, _ := a.config(false)
	c, err := tls.Dial("tcp", listener.Addr().String(), cc)
	if err == nil {
		c.Close()
		t.Fatal("unapproved server accepted")
	}
	<-done
}
func TestProtocolVersionAndPlaintextFailClosed(t *testing.T) {
	a, b := testTrust(t), testTrust(t)
	approve(t, a, b)
	approve(t, b, a)
	agent := newAgent("test", "test", "", b)
	for _, version := range []int{1, 99} {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		done := make(chan struct{})
		go func() {
			defer close(done)
			c, err := listener.Accept()
			if err == nil {
				agent.handleInbound(c)
			}
		}()
		config, _ := a.config(false)
		client, err := tls.Dial("tcp", listener.Addr().String(), config)
		if err != nil {
			t.Fatal(err)
		}
		client.SetDeadline(time.Now().Add(2 * time.Second))
		raw, _ := json.Marshal(Hello{Kind: "hello", Version: version, Mode: "stream"})
		if err = writeFrame(client, raw); err != nil {
			t.Fatal(err)
		}
		if _, err = readFrame(client); err == nil {
			t.Fatal("unsupported hello accepted")
		}
		client.Close()
		listener.Close()
		<-done
		if len(agent.registry.snapshot()) != 0 {
			t.Fatal("unsupported peer registered")
		}
	}
	left, right := net.Pipe()
	done := make(chan struct{})
	go func() { defer close(done); agent.handleInbound(left) }()
	right.SetDeadline(time.Now().Add(time.Second))
	writeFrame(right, []byte(`{"type":"text"}`))
	right.Close()
	<-done
	if len(agent.registry.snapshot()) != 0 {
		t.Fatal("plaintext registered")
	}
}

func TestActualAgentDialAndInboundUsePairedHello(t *testing.T) {
	a, b := testTrust(t), testTrust(t)
	approve(t, a, b)
	approve(t, b, a)
	sender := newAgent("sender", "sender", "", a)
	receiver := newAgent("receiver", "receiver", "", b)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	done := make(chan struct{})
	go func() {
		defer close(done)
		c, err := listener.Accept()
		if err == nil {
			receiver.handleInbound(c)
		}
	}()
	if err = sender.dial(context.Background(), peerSpec{Address: listener.Addr().String()}, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 100 && len(receiver.registry.snapshot()) == 0; i++ {
		time.Sleep(time.Millisecond * 5)
	}
	if len(sender.registry.snapshot()) != 1 || len(receiver.registry.snapshot()) != 1 {
		t.Fatal("paired stream missing")
	}
	for _, pc := range sender.registry.snapshot() {
		sender.registry.drop(pc)
	}
	<-done
}
func TestMalformedHashNeverReachesClipboard(t *testing.T) {
	path := filepath.Join(t.TempDir(), "set")
	marker := filepath.Join(t.TempDir(), "called")
	os.WriteFile(path, []byte("#!/bin/sh\ntouch \"$CLIPBAR_TEST_MARKER\"\n"), 0700)
	t.Setenv("CLIPBAR_TEST_MARKER", marker)
	previous := termuxClipSetPath
	termuxClipSetPath = path
	defer func() { termuxClipSetPath = previous }()
	a := newAgent("test", "test", "", testTrust(t))
	value := "synthetic"
	a.applyItem(Item{Type: "text", Content: &value, ContentHash: "../invalid"}, "test")
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("malformed item reached clipboard")
	}
}
