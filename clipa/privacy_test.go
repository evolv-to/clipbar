package main

import (
	"bufio"
	"bytes"
	"log"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func captureTestLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	var output bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&output)
	t.Cleanup(func() { log.SetOutput(previous) })
	return &output
}

func TestReceivePreservesClipboardContentWithoutLoggingIt(t *testing.T) {
	logs := captureTestLogs(t)
	directory := t.TempDir()
	outputPath := filepath.Join(directory, "clipboard-output")
	setter := filepath.Join(directory, "clipboard-set")
	if err := os.WriteFile(setter, []byte("#!/bin/sh\ncat > \"$CLIPBAR_TEST_OUTPUT\"\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CLIPBAR_TEST_OUTPUT", outputPath)
	previous := termuxClipSetPath
	termuxClipSetPath = setter
	t.Cleanup(func() { termuxClipSetPath = previous })
	content := "synthetic-private-clipboard-content"
	preview := "synthetic-private-clipboard-preview"
	a := newAgent("test-self", "test-system", "")
	a.applyItem(Item{Type: "text", Content: &content, Preview: preview, ContentHash: sha256Hex([]byte(content))}, "test-peer")
	stored, err := os.ReadFile(outputPath)
	if err != nil || string(stored) != content {
		t.Fatalf("clipboard delivery failed: %v", err)
	}
	if strings.Contains(logs.String(), content) || strings.Contains(logs.String(), preview) {
		t.Fatal("clipboard content appeared in logs")
	}
}

func TestBroadcastPreservesPayloadWithoutLoggingIt(t *testing.T) {
	logs := captureTestLogs(t)
	left, right := net.Pipe()
	t.Cleanup(func() { left.Close(); right.Close() })
	left.SetDeadline(time.Now().Add(5 * time.Second))
	right.SetDeadline(time.Now().Add(5 * time.Second))
	a := newAgent("test-self", "test-system", "")
	a.registry.adopt(&peerConn{ip: "test-peer", hostname: "test-peer", conn: left, w: bufio.NewWriter(left)})
	payload := []byte(`{"type":"text","content":"synthetic-private-payload"}`)
	type result struct {
		data []byte
		err  error
	}
	received := make(chan result, 1)
	go func() {
		data, err := readFrame(right)
		received <- result{data, err}
	}()
	a.broadcast(payload)
	r := <-received
	if r.err != nil || !bytes.Equal(r.data, payload) {
		t.Fatalf("peer delivery failed: %v", r.err)
	}
	if strings.Contains(logs.String(), "synthetic-private-payload") {
		t.Fatal("clipboard payload appeared in logs")
	}
}
