# Pair devices before syncing

ClipBar 0.5.0 introduces protocol 2. Upgrade all participating Macs and the Go
Termux companion together. Old clients cannot sync with protocol 2. LAN discovery and Tailscale still find addresses, but
clipboard data is shared only after **both devices explicitly approve each other**.
The separate experimental Kotlin Android app is not compatible with this release.

## Macs

From the repository root, export the public certificate and display this device's
SHA-256 fingerprint:

```bash
uv run --directory clipd python clipd.py identity --export "$HOME/Desktop/clipbar-device.crt"
```

Transfer that `.crt` file to the other device. Confirm its fingerprint against the
value shown on the original device's screen or another trusted channel. Do not
trust a fingerprint supplied only alongside an untrusted downloaded certificate.
Never transfer `identity.key` or the entire configuration directory.

On the receiving Mac, import the certificate using the confirmed fingerprint and
a label you recognize:

```bash
uv run --directory clipd python clipd.py pair --certificate /path/to/peer.crt --fingerprint CONFIRMED_SHA256 --name "My other device"
```

Repeat in the other direction. Pairing and revocation reload automatically; no
restart is needed. LAN peers should then appear in the menu. For internet sync,
keep the devices on your tailnet and allow their TCP/2029 traffic through your ACLs.

List or revoke a device:

```bash
uv run --directory clipd python clipd.py paired
uv run --directory clipd python clipd.py unpair PEER_SHA256
```

Revocation stops new transfers and closes idle streams within about one second.
It cannot erase content already copied to that device. Certificates expire after
ten years; replacing a device or its identity requires exchanging and confirming
a new certificate. Back up your private state securely if you need to preserve
pairing identity across a reinstall. Uninstall preserves that state by default.

## Android through Termux

Build/install the updated tracked `clipa` companion. Export and approve with:

```bash
clipa --identity --export "$HOME/clipbar-phone.crt"
clipa --pair /path/to/mac.crt --fingerprint CONFIRMED_SHA256 --name "My Mac"
clipa --unpair PEER_SHA256
```

Also approve the phone's certificate on the Mac. Keep `~/.config/clipa/peers.json`
as an address list for the Macs; it grants no trust. Termux supports text sync.
Termux:API does not expose macOS sensitive-pasteboard markers, so its capture
limitations differ; do not assume a password copied on Android is excluded.

## Private state and testing

macOS state is in `~/.config/clipd/`; Termux state is in `~/.config/clipa/`.
`identity.crt` is public; `identity.key` is private; `trust.json` contains approved
public certificates and local labels. These files must not be committed.

For isolated development, set `CLIPBAR_CONFIG_DIR` for the Mac daemon/app, and use
`clipa --config /path/to/private-state`. `CLIPBAR_NO_DISCOVERY=1`,
`CLIPBAR_BIND_HOST=127.0.0.1`, and `CLIPBAR_PORT=0` isolate Mac networking;
`clipa --listen 127.0.0.1:0` and an empty private `peers.json` isolate Termux tests.
Use synthetic clipboard content only. These test settings do not change the
installed app unless explicitly applied to that process.
