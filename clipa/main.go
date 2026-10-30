// clipa — Android (Termux) companion to ClipBar.
//
// Bridges Android's system clipboard to a tailnet of ClipBar peers using
// the same wire protocol the macOS daemon speaks (see ../PROTOCOL.md).
//
// What it does:
//   - Polls termux-clipboard-get every ~1.5s. On change, frames the item
//     and pushes it to every open peer connection.
//   - Maintains persistent dial-with-backoff connections to peers listed in
//     ~/.config/clipa/peers.json. Falls back to `tailscale status --json`
//     if that file is missing AND the CLI is available.
//   - Listens on :2029 so Macs that can dial us can push items in.
//   - On receive, writes the item to Android's clipboard via
//     termux-clipboard-set.
//   - Suppresses the next poll round-trip after a write so we don't echo
//     the item back to the network.
//
// Why static peers.json is the default discovery path: the official
// Tailscale Android app handles tunneling system-wide (so dialing 100.x
// IPs Just Works from Termux), but the `tailscale` CLI in Termux is a
// separate install that may or may not be present. Static config is
// reliable and trivial to maintain.

package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"crypto/sha256"
)

const (
	listenPort       = 2029
	protocolVersion  = 2
	maxFrameBytes    = 25 * 1024 * 1024
	pollInterval     = 1500 * time.Millisecond
	dialInterval     = 10 * time.Second
	dialTimeout      = 5 * time.Second
	dialBackoffStart = 5 * time.Second
	dialBackoffMax   = 120 * time.Second
)

// ---------- frame format ----------

func writeFrame(w io.Writer, payload []byte) error {
	if len(payload) > maxFrameBytes {
		return fmt.Errorf("payload too large: %d", len(payload))
	}
	hdr := make([]byte, 4)
	binary.BigEndian.PutUint32(hdr, uint32(len(payload)))
	if _, err := w.Write(hdr); err != nil {
		return err
	}
	_, err := w.Write(payload)
	return err
}

func readFrame(r io.Reader) ([]byte, error) {
	hdr := make([]byte, 4)
	if _, err := io.ReadFull(r, hdr); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(hdr)
	if n > maxFrameBytes {
		return nil, fmt.Errorf("frame too large: %d", n)
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(r, buf); err != nil {
		return nil, err
	}
	return buf, nil
}

// ---------- item + control schema ----------

type Item struct {
	Type        string  `json:"type"`
	Content     *string `json:"content,omitempty"`
	ImageData   *string `json:"image_data,omitempty"`
	ImagePath   *string `json:"image_path,omitempty"`
	Preview     string  `json:"preview"`
	ContentHash string  `json:"content_hash"`
	CreatedAt   float64 `json:"created_at"`
	SourceMac   string  `json:"source_mac"`
}

type Hello struct {
	Kind           string `json:"_kind"`
	Hostname       string `json:"hostname"`
	SystemHostname string `json:"system_hostname,omitempty"`
	Version        int    `json:"version"`
	Mode           string `json:"mode"`
}

// envelope used to peek at unknown frames
type envelope struct {
	Kind string `json:"_kind,omitempty"`
}

// ---------- discovery ----------

type peerSpec struct {
	Hostname string `json:"hostname"`
	Address  string `json:"address"` // IP or "host:port"; if no port, listenPort is appended
}

func loadStaticPeers(path string) ([]peerSpec, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var peers []peerSpec
	if err := json.Unmarshal(b, &peers); err != nil {
		return nil, err
	}
	for i := range peers {
		if !strings.Contains(peers[i].Address, ":") || strings.HasPrefix(peers[i].Address, "[") {
			// add default port unless it already looks like host:port
			if _, _, err := net.SplitHostPort(peers[i].Address); err != nil {
				peers[i].Address = fmt.Sprintf("%s:%d", peers[i].Address, listenPort)
			}
		}
	}
	return peers, nil
}

func loadTailscalePeers() ([]peerSpec, error) {
	bin := findBinary("tailscale")
	if bin == "" {
		return nil, errors.New("tailscale binary not found on PATH")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, bin, "status", "--json").Output()
	if err != nil {
		return nil, err
	}
	var status struct {
		Peer map[string]struct {
			HostName     string   `json:"HostName"`
			TailscaleIPs []string `json:"TailscaleIPs"`
			Online       bool     `json:"Online"`
		} `json:"Peer"`
	}
	if err := json.Unmarshal(out, &status); err != nil {
		return nil, err
	}
	var peers []peerSpec
	for _, p := range status.Peer {
		if !p.Online {
			continue
		}
		var ipv4 string
		for _, ip := range p.TailscaleIPs {
			if !strings.Contains(ip, ":") {
				ipv4 = ip
				break
			}
		}
		if ipv4 == "" || p.HostName == "" {
			continue
		}
		peers = append(peers, peerSpec{
			Hostname: p.HostName,
			Address:  fmt.Sprintf("%s:%d", ipv4, listenPort),
		})
	}
	return peers, nil
}

// ---------- peer connection registry ----------

type peerConn struct {
	ip          string // address only
	fingerprint string
	initiated   bool
	hostname    string // display label
	conn        net.Conn
	w           *bufio.Writer
	mu          sync.Mutex // serialise frame writes
}

func (pc *peerConn) sendItem(payload []byte) error {
	pc.mu.Lock()
	defer pc.mu.Unlock()
	pc.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
	defer pc.conn.SetWriteDeadline(time.Time{})
	if err := writeFrame(pc.w, payload); err != nil {
		return err
	}
	return pc.w.Flush()
}

// Registry keys are approved certificate fingerprints. Addresses and remote
// hostnames are never authorization identities.
type registry struct {
	identity    string
	mu          sync.Mutex
	connections map[string]*peerConn // ip -> conn
}

func newRegistry() *registry {
	return &registry{connections: make(map[string]*peerConn)}
}

// adopt returns true if the peer was added, false if a connection for the
// same IP already exists (in which case the new conn is closed and dropped).
func (r *registry) adopt(pc *peerConn) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if pc.fingerprint == "" {
		pc.conn.Close()
		return false
	}
	if existing, exists := r.connections[pc.fingerprint]; exists {
		preferOutbound := r.identity < pc.fingerprint
		if pc.initiated != preferOutbound || existing.initiated == preferOutbound {
			pc.conn.Close()
			return false
		}
		existing.conn.Close()
	}
	r.connections[pc.fingerprint] = pc
	return true
}

func (r *registry) drop(pc *peerConn) {
	r.mu.Lock()
	if r.connections[pc.fingerprint] == pc {
		delete(r.connections, pc.fingerprint)
	}
	r.mu.Unlock()
	_ = pc.conn.Close()
}

func (r *registry) hasIP(ip string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, peer := range r.connections {
		if peer.ip == ip {
			return true
		}
	}
	return false
}

func (r *registry) snapshot() []*peerConn {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*peerConn, 0, len(r.connections))
	for _, pc := range r.connections {
		out = append(out, pc)
	}
	return out
}

// ---------- agent ----------

type agent struct {
	listenAddress  string
	trust          *trustStore
	selfHostname   string
	systemHostname string
	registry       *registry
	peersFile      string

	// suppression: when we just wrote a value to Android's clipboard,
	// the next poll will read it back unchanged. Skip re-broadcasting it.
	suppressMu   sync.Mutex
	suppressHash string
}

func newAgent(self, system, peersFile string, trust *trustStore) *agent {
	registry := newRegistry()
	if trust != nil {
		registry.identity = trust.identity
	}
	return &agent{
		trust:          trust,
		listenAddress:  "0.0.0.0:2029",
		selfHostname:   self,
		systemHostname: system,
		registry:       registry,
		peersFile:      peersFile,
	}
}

func (a *agent) helloFrame() ([]byte, error) {
	return json.Marshal(Hello{
		Kind:           "hello",
		Hostname:       a.selfHostname,
		SystemHostname: a.systemHostname,
		Version:        protocolVersion,
		Mode:           "stream",
	})
}

// ---------- listener (acceptor side) ----------

func (a *agent) serve(ctx context.Context) error {
	// KeepAlive on incoming sockets so the kernel can prune dead peers.
	lc := net.ListenConfig{KeepAlive: 30 * time.Second}
	addr := a.listenAddress
	ln, err := lc.Listen(ctx, "tcp", addr)
	if err != nil {
		return err
	}
	log.Printf("listening on %s", ln.Addr().String())
	go func() {
		<-ctx.Done()
		_ = ln.Close()
		for _, peer := range a.registry.snapshot() {
			a.registry.drop(peer)
		}
	}()
	slots := make(chan struct{}, 64)
	for {
		c, err := ln.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return nil
			}
			log.Printf("accept: %v", err)
			continue
		}
		select {
		case slots <- struct{}{}:
			go func() { defer func() { <-slots }(); a.handleInbound(c) }()
		default:
			c.Close()
		}
	}
}

// handleInbound reads frames from a connection initiated by a peer. The
// first frame is expected to be a hello; we register the connection so
// our broadcast loop can push items back over it.
func (a *agent) handleInbound(raw net.Conn) {
	defer raw.Close()
	if a.trust == nil {
		return
	}
	config, err := a.trust.config(true)
	if err != nil {
		return
	}
	c := tls.Server(raw, config)
	c.SetDeadline(time.Now().Add(10 * time.Second))
	if err = c.Handshake(); err != nil {
		return
	}
	identity, label, err := a.trust.authorize(c)
	if err != nil {
		return
	}
	br := bufio.NewReader(c)
	first, err := readFrame(br)
	if err != nil {
		return
	}
	var hello Hello
	if json.Unmarshal(first, &hello) != nil || hello.Kind != "hello" || hello.Version != protocolVersion || (hello.Mode != "once" && hello.Mode != "stream") {
		return
	}
	response, _ := json.Marshal(Hello{Kind: "hello", Version: protocolVersion, Mode: hello.Mode})
	if writeFrame(c, response) != nil {
		return
	}
	if hello.Mode == "once" {
		payload, err := readFrame(br)
		if err != nil {
			return
		}
		if _, _, err = a.trust.authorize(c); err == nil && a.ingestRaw(payload, label) {
			var item Item
			json.Unmarshal(payload, &item)
			ack, _ := json.Marshal(map[string]string{"_kind": "ack", "content_hash": item.ContentHash})
			writeFrame(c, ack)
		}
		return
	}
	c.SetDeadline(time.Time{})
	pc := &peerConn{ip: remoteIPOf(c), fingerprint: identity, hostname: label, conn: c, w: bufio.NewWriter(c)}
	if !a.registry.adopt(pc) {
		return
	}
	a.readLoop(pc, br)
}

func remoteIPOf(c net.Conn) string {
	if a, ok := c.RemoteAddr().(*net.TCPAddr); ok {
		return a.IP.String()
	}
	return ""
}

// ---------- dialer (initiator side) ----------

func (a *agent) dialLoop(ctx context.Context) {
	backoff := make(map[string]time.Duration)
	nextAttempt := make(map[string]time.Time)
	for {
		peers := a.discoverPeers()
		now := time.Now()
		for _, p := range peers {
			ip := ipFromAddress(p.Address)
			if ip == "" {
				continue
			}
			if a.registry.hasIP(ip) {
				continue // already connected (in either direction)
			}
			if t, ok := nextAttempt[ip]; ok && now.Before(t) {
				continue
			}
			if err := a.dial(ctx, p, ip); err != nil {
				prev := backoff[ip]
				if prev == 0 {
					prev = dialBackoffStart
				} else {
					prev *= 2
					if prev > dialBackoffMax {
						prev = dialBackoffMax
					}
				}
				backoff[ip] = prev
				nextAttempt[ip] = now.Add(prev)
				log.Printf("dial %s (%s) failed: %v", p.Hostname, p.Address, err)
			} else {
				delete(backoff, ip)
				delete(nextAttempt, ip)
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(dialInterval):
		}
	}
}

func (a *agent) dial(ctx context.Context, p peerSpec, ip string) error {
	if a.trust == nil {
		return errors.New("paired trust is required")
	}
	config, err := a.trust.config(false)
	if err != nil {
		return err
	}
	dctx, cancel := context.WithTimeout(ctx, dialTimeout)
	defer cancel()
	d := tls.Dialer{NetDialer: &net.Dialer{KeepAlive: 30 * time.Second}, Config: config}
	c, err := d.DialContext(dctx, "tcp", p.Address)
	if err != nil {
		return err
	}
	success := false
	defer func() {
		if !success {
			c.Close()
		}
	}()
	c.SetDeadline(time.Now().Add(10 * time.Second))
	identity, label, err := a.trust.authorize(c)
	if err != nil {
		return err
	}
	hello, err := a.helloFrame()
	if err != nil {
		return err
	}
	if err = writeFrame(c, hello); err != nil {
		return err
	}
	br := bufio.NewReader(c)
	raw, err := readFrame(br)
	if err != nil {
		return err
	}
	var reply Hello
	if json.Unmarshal(raw, &reply) != nil || reply.Kind != "hello" || reply.Version != protocolVersion || reply.Mode != "stream" {
		return errors.New("paired protocol version 2 hello required")
	}
	c.SetDeadline(time.Time{})
	pc := &peerConn{ip: ip, fingerprint: identity, hostname: label, initiated: true, conn: c, w: bufio.NewWriter(c)}
	if !a.registry.adopt(pc) {
		return nil
	}
	success = true
	go a.readLoop(pc, br)
	return nil
}

func ipFromAddress(addr string) string {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return addr
	}
	return host
}

func (a *agent) discoverPeers() []peerSpec {
	if peers, err := loadStaticPeers(a.peersFile); err == nil {
		return peers
	}
	if peers, err := loadTailscalePeers(); err == nil {
		return peers
	}
	return nil
}

// ---------- read loop ----------

func (a *agent) readLoop(pc *peerConn, br *bufio.Reader) {
	defer func() {
		log.Printf("disconnected from %s @ %s", pc.hostname, pc.ip)
		a.registry.drop(pc)
	}()
	for {
		first, err := br.ReadByte()
		if err != nil {
			return
		}
		pc.conn.SetReadDeadline(time.Now().Add(30 * time.Second))
		raw, err := readFrame(io.MultiReader(strings.NewReader(string([]byte{first})), br))
		pc.conn.SetReadDeadline(time.Time{})
		if err != nil {
			return
		}
		if a.trust == nil {
			return
		}
		if _, _, err = a.trust.authorize(pc.conn); err != nil {
			return
		}
		var env envelope
		if json.Unmarshal(raw, &env) == nil && env.Kind != "" {
			// ignore unknown control frames (forward-compat)
			continue
		}
		a.ingestRaw(raw, fmt.Sprintf("tailscale:%s", pc.hostname))
	}
}

func (a *agent) ingestRaw(raw []byte, sourceLabel string) bool {
	var item Item
	if err := json.Unmarshal(raw, &item); err != nil {
		log.Printf("rejected malformed clipboard item")
		return false
	}
	return a.applyItem(item, sourceLabel)
}

func (a *agent) applyItem(item Item, sourceLabel string) bool {
	if item.Type == "text" && item.Content != nil {
		if len(*item.Content) > 1024*1024 || sha256Hex([]byte(*item.Content)) != item.ContentHash {
			return false
		}
		a.suppressMu.Lock()
		a.suppressHash = item.ContentHash
		a.suppressMu.Unlock()
		if err := termuxClipboardSet(*item.Content); err != nil {
			log.Printf("termux-clipboard-set: %v", err)
			return false
		}
		log.Printf("received clipboard text from %s", sourceLabel)
		return true
	}
	// Images currently not pushed to Android's clipboard via termux-api
	// reliably across Android versions. We drop them
	// silently and rely on Mac peers to keep the canonical history.
	log.Printf("received unsupported clipboard item (skipped)")
	return false
}

// ---------- clipboard polling (sender side) ----------

func (a *agent) pollLoop(ctx context.Context) {
	var lastHash string
	var lastErrLog time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(pollInterval):
		}
		text, err := termuxClipboardGet()
		if err != nil {
			// Rate-limit so a missing APK doesn't spam the log.
			if time.Since(lastErrLog) > time.Minute {
				log.Printf("termux-clipboard-get: %v", err)
				lastErrLog = time.Now()
			}
			continue
		}
		if text == "" || len(text) > 1024*1024 {
			continue
		}
		hash := sha256Hex([]byte(text))
		a.suppressMu.Lock()
		suppress := hash == a.suppressHash
		a.suppressMu.Unlock()
		if suppress {
			lastHash = hash
			continue
		}
		if hash == lastHash {
			continue
		}
		lastHash = hash
		preview := text
		if len(preview) > 120 {
			preview = preview[:120]
		}
		preview = strings.ReplaceAll(preview, "\n", " ")
		item := Item{
			Type:        "text",
			Content:     &text,
			Preview:     strings.TrimSpace(preview),
			ContentHash: hash,
			CreatedAt:   float64(time.Now().UnixNano()) / 1e9,
			SourceMac:   a.selfHostname,
		}
		raw, err := json.Marshal(item)
		if err != nil {
			continue
		}
		a.broadcast(raw)
	}
}

func (a *agent) broadcast(payload []byte) {
	conns := a.registry.snapshot()
	if len(conns) == 0 {
		return
	}
	sent := 0
	for _, pc := range conns {
		if a.trust == nil {
			a.registry.drop(pc)
			continue
		}
		if _, _, err := a.trust.authorize(pc.conn); err != nil {
			a.registry.drop(pc)
			continue
		}
		if err := pc.sendItem(payload); err != nil {
			log.Printf("send to %s failed: %v", pc.hostname, err)
			a.registry.drop(pc)
			continue
		}
		sent++
	}
	log.Printf("broadcast clipboard item to %d peer(s)", sent)
}

// ---------- termux-api shims ----------

// Termux binary discovery. We avoid exec.LookPath / exec.Command(basename)
// because Go's runtime calls faccessat2 internally, which Android's seccomp
// filter rejects with SIGSYS, killing the process. os.Stat-based PATH walks
// are safe.
var (
	termuxClipGetPath string
	termuxClipSetPath string
)

func resolveTermuxBinaries() {
	termuxClipGetPath = findBinary("termux-clipboard-get")
	termuxClipSetPath = findBinary("termux-clipboard-set")
}

// Timeouts are critical: termux-clipboard-{get,set} are thin shims around
// `am broadcast` to the Termux:API Android service. If the Termux:API APK
// isn't installed on the phone, the IPC has no receiver and the call
// blocks forever. Bound it so a missing APK becomes a loud error, not a
// silent stall.
//
// Process-group killing: the shim is a shell script that fork-execs
// `termux-api`. SIGKILLing the shim alone leaves termux-api orphaned with
// the stdout pipe still open, so Output()'s read goroutine never sees EOF
// and Wait() hangs. Put the child in its own pgid and kill the group on
// cancellation.
const termuxAPITimeout = 4 * time.Second

func runTermuxAPI(stdin string, paths ...string) ([]byte, error) {
	bin := paths[0]
	if bin == "" {
		return nil, errors.New("termux-api binary not on PATH")
	}
	ctx, cancel := context.WithTimeout(context.Background(), termuxAPITimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		// Negative pid = "kill this process group". Reaches descendants
		// the shim shell forked off (e.g. termux-api itself).
		if cmd.Process != nil {
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		return os.ErrProcessDone
	}
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	out, err := cmd.Output()
	if ctx.Err() == context.DeadlineExceeded {
		return nil, errTermuxAPIMissing
	}
	return out, err
}

func termuxClipboardGet() (string, error) {
	out, err := runTermuxAPI("", termuxClipGetPath)
	if err != nil {
		return "", err
	}
	return string(out), nil
}

func termuxClipboardSet(text string) error {
	_, err := runTermuxAPI(text, termuxClipSetPath)
	return err
}

var errTermuxAPIMissing = errors.New("termux-clipboard-* timed out — Termux:API APK is probably not installed (https://f-droid.org/en/packages/com.termux.api/)")

func findBinary(name string) string {
	pathEnv := os.Getenv("PATH")
	for _, dir := range filepath.SplitList(pathEnv) {
		if dir == "" {
			continue
		}
		p := filepath.Join(dir, name)
		if fi, err := os.Stat(p); err == nil && !fi.IsDir() {
			return p
		}
	}
	// Fall back to known absolute Termux locations in case PATH is empty.
	for _, p := range []string{
		"/data/data/com.termux/files/usr/bin/" + name,
		"/usr/local/bin/" + name,
	} {
		if fi, err := os.Stat(p); err == nil && !fi.IsDir() {
			return p
		}
	}
	return ""
}

// ---------- helpers ----------

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	const hex = "0123456789abcdef"
	out := make([]byte, len(sum)*2)
	for i, v := range sum {
		out[i*2] = hex[v>>4]
		out[i*2+1] = hex[v&0x0f]
	}
	return string(out)
}

func detectHostname() string {
	// Prefer tailnet hostname if we can read it.
	if bin := findBinary("tailscale"); bin != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if out, err := exec.CommandContext(ctx, bin, "status", "--json").Output(); err == nil {
			var s struct {
				Self struct {
					HostName string `json:"HostName"`
				} `json:"Self"`
			}
			if json.Unmarshal(out, &s) == nil && s.Self.HostName != "" {
				return s.Self.HostName
			}
		}
	}
	if h, err := os.Hostname(); err == nil && h != "" {
		return h
	}
	return "android-clipa"
}

func defaultPeersFile() string {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(home, ".config", "clipa", "peers.json")
}

// _ = base64 keeps the import pinned for future image support.
var _ = base64.StdEncoding

// ---------- entrypoint ----------

func main() {
	listenFlag := flag.String("listen", "0.0.0.0:2029", "TLS listen address")
	hostnameFlag := flag.String("hostname", "", "override our reported hostname (default: tailnet HostName, else os.Hostname())")
	configFlag := flag.String("config", filepath.Dir(defaultPeersFile()), "private state directory")
	identityFlag := flag.Bool("identity", false, "show certificate fingerprint (and export with --export)")
	exportFlag := flag.String("export", "", "export our public certificate to this file")
	pairFlag := flag.String("pair", "", "import this peer public certificate")
	fingerprintFlag := flag.String("fingerprint", "", "peer SHA-256 fingerprint confirmed on its own screen")
	nameFlag := flag.String("name", "", "local label for the paired device")
	unpairFlag := flag.String("unpair", "", "remove this certificate fingerprint")
	peersFlag := flag.String("peers", "", "path to peers.json (static peer list)")
	logFlag := flag.String("log", "", "if set, append logs to this file (otherwise stderr)")
	flag.Parse()
	if *peersFlag == "" {
		*peersFlag = filepath.Join(*configFlag, "peers.json")
	}
	trust, err := newTrust(*configFlag)
	if err != nil {
		pairingError(err)
	}
	if *identityFlag {
		fmt.Println(trust.identity)
		if *exportFlag != "" {
			data, err := privateRead(filepath.Join(*configFlag, "identity.crt"))
			if err == nil {
				err = os.WriteFile(*exportFlag, data, 0600)
			}
			if err != nil {
				pairingError(err)
			}
		}
		return
	}
	if *pairFlag != "" {
		data, err := os.ReadFile(*pairFlag)
		if err == nil {
			err = trust.pair(data, *fingerprintFlag, *nameFlag)
		}
		if err != nil {
			pairingError(err)
		}
		fmt.Println("Peer paired; confirm this device on the other peer too.")
		return
	}
	if *unpairFlag != "" {
		if err = trust.unpair(*unpairFlag); err != nil {
			pairingError(err)
		}
		fmt.Println("Peer removed.")
		return
	}

	if *logFlag != "" {
		f, err := os.OpenFile(*logFlag, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
		if err == nil {
			log.SetOutput(f)
		}
	}
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	self := *hostnameFlag
	if self == "" {
		self = detectHostname()
	}
	system, _ := os.Hostname()
	log.Printf("clipa starting (self=%s, system=%s, peers=%s)", self, system, *peersFlag)

	resolveTermuxBinaries()
	if termuxClipGetPath == "" || termuxClipSetPath == "" {
		log.Printf("WARNING: termux-clipboard-get/set not found on PATH; install termux-api (`pkg install termux-api` and the Termux:API APK)")
	}
	// One-shot probe at startup so a missing Termux:API APK is loud
	// instead of getting absorbed by the rate-limited error path.
	go func() {
		if _, err := termuxClipboardGet(); err != nil {
			log.Printf("startup probe: %v", err)
		}
	}()

	if _, err := os.Stat(*peersFlag); err != nil {
		log.Printf("note: no peers.json at %s — falling back to `tailscale status --json` (if available)", *peersFlag)
	} else {
		if peers, err := loadStaticPeers(*peersFlag); err == nil {
			names := make([]string, 0, len(peers))
			for _, p := range peers {
				names = append(names, fmt.Sprintf("%s@%s", p.Hostname, p.Address))
			}
			sort.Strings(names)
			log.Printf("static peers: %s", strings.Join(names, ", "))
		}
	}

	a := newAgent(self, system, *peersFlag, trust)
	a.listenAddress = *listenFlag
	ctx := context.Background()

	go func() {
		if err := a.serve(ctx); err != nil {
			log.Fatalf("listener: %v", err)
		}
	}()
	go a.dialLoop(ctx)
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for range ticker.C {
			for _, peer := range a.registry.snapshot() {
				if _, _, err := trust.authorize(peer.conn); err != nil {
					a.registry.drop(peer)
				}
			}
		}
	}()
	a.pollLoop(ctx)
}
