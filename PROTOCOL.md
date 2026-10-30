# ClipBar sync wire protocol

This is the on-the-wire protocol shared by the macOS daemon (`clipd/clipd.py`)
and the Android Termux agent (`clipa/main.go`). Anyone implementing a new
ClipBar client should match this spec exactly.

Protocol version: **1**.

## Transport

- Plain TCP. No TLS.
  - On LAN: rely on local network trust (same as the existing zero-config
    behaviour).
  - On Tailscale: rely on WireGuard end-to-end encryption between tailnet
    peers. Tailscale ACLs gate who may dial whom.
- Listen port: **2029** on every device that participates in sync.
- Connections are bidirectional once the dialer announces itself with a
  hello frame (see below). This matters when ACLs are asymmetric — the
  side that can dial holds the duplex pipe.

## Frame format

Every message is a length-prefixed JSON object:

```
+--------+----------------------+
| length |     JSON payload     |
| u32 BE |    UTF-8 bytes       |
+--------+----------------------+
```

- `length` is an unsigned 32-bit big-endian integer giving the byte length
  of the JSON payload that follows.
- Maximum frame size: **25 MiB**. Receivers must drop the connection if
  they read a length larger than this.
- The JSON object always has a `_kind` field (control frames) **or** the
  shape of an item frame (everything else). Both are described below.

## Control frames

### `hello` (required as the first frame on a long-lived connection)

```json
{
  "_kind": "hello",
  "hostname": "<tailnet-hostname>",
  "system_hostname": "<os-hostname>",
  "version": 1
}
```

- `hostname`: the dialer's identity for routing — the tailnet hostname
  shown by `tailscale status`. On Macs without the Tailscale CLI this
  falls back to `socket.gethostname()`. On the Termux agent it's the
  device's tailnet hostname (or whatever `--hostname` overrides it to).
- `system_hostname` (optional): the dialer's OS hostname (e.g. the
  Bonjour name `host.local`). Used by the receiver to deduplicate
  against LAN-discovered peers — mDNS announces the OS hostname while
  tailnet announces the tailnet name, and both can refer to the same
  physical device.
- `version`: protocol version. Receivers should accept any value `<=` their
  own, ignore unknown fields, and refuse strictly higher major versions.

A connection without a hello is treated as a one-shot LAN broadcast: the
receiver reads item frames until EOF, ingests each, and closes. This keeps
backwards compatibility with the original LAN sync.

## Item frames

Every other frame is a clipboard item. Required fields:

| Field          | Type     | Description                                              |
|----------------|----------|----------------------------------------------------------|
| `type`         | string   | `"text"` or `"image"`.                                   |
| `content`      | string?  | Present iff `type == "text"`. UTF-8 plain text.          |
| `image_data`   | string?  | Present iff `type == "image"`. Base64-encoded PNG bytes. |
| `image_path`   | string?  | Sender-local file path (informational, may be ignored).  |
| `preview`      | string   | Short human-readable preview, ≤ 120 chars.               |
| `content_hash` | string   | SHA-256 hex digest of the canonical content.             |
| `created_at`   | float    | Unix timestamp at the source.                            |
| `source_mac`   | string   | Source device label (free-form, used in the UI).         |

Hashing rules:

- Text: `sha256(content_bytes_utf8).hexdigest()`.
- Image: `sha256(raw_png_bytes).hexdigest()`.

Receivers ingest items into their local store keyed by `content_hash` with
a `UNIQUE` constraint, so duplicates are silently dropped. This is the
conflict-resolution strategy: last-write-wins by `created_at`, dedup by
`content_hash`.

Senders must omit `image_data` for text items and must include it for
image items (otherwise the receiver has nothing to write to disk).

## Connection lifecycle

```
initiator                              acceptor
  |                                       |
  | TCP connect → :2029                   |
  |─────────────────────────────────────► |
  |                                       |
  | frame: hello                          |
  |─────────────────────────────────────► | register peer
  |                                       |
  | frame: item (optional, any time)      |
  |─────────────────────────────────────► |
  |                                       |
  |     frame: item (optional, any time)  |
  |◄───────────────────────────────────── |
  |                                       |
  ⋮ (kept open, idle is fine)             ⋮
  |                                       |
  | TCP close / timeout                   |
  ⊥                                       ⊥
```

- The initiator should keep the connection open indefinitely. Send any new
  local clipboard item as an item frame. When the connection drops,
  reconnect with exponential backoff (start at 5s, double on each failure,
  cap at 120s).
- The acceptor uses the same socket to push its own clipboard items back.
  This is what makes asymmetric-ACL tailnets work without requiring a
  reverse ACL edge: only the dialer side needs `tag:X → tag:Y` allowed.
- Either side may close the connection at any time. No graceful-shutdown
  frame is required.

## Discovery

- LAN: mDNS service type `_clipd._tcp.local.`, TXT records:
  - `hostname`: OS hostname of the advertiser.
  - `tailnet_hostname` (optional): tailnet hostname when known. Helps
    receivers dedup against tailnet-discovered peers.
- Tailscale: poll `tailscale status --json` on a 10-second cadence. Filter
  to `Peer[*].Online == true`. Use the first IPv4 from `TailscaleIPs`.
  Treat dial failure to a discovered peer as "peer doesn't run ClipBar"
  and back off silently.

## Toggles

ClipBar exposes one user-facing toggle, mirrored as a flag file:

- `~/.config/clipd/.tailscale_off` — touch to disable Tailscale sync, delete
  to re-enable. The macOS daemon checks this in its dial loop and broadcast
  path. Other clients (Termux agent) can choose to mirror this with their
  own equivalent (`$HOME/.config/clipa/disabled` or similar).

## Versioning policy

- Add new optional fields freely. Receivers ignore unknown fields.
- Add new control-frame `_kind`s freely. Receivers ignore `_kind`s they
  don't recognise.
- Bump `version` for breaking changes only (changing existing field
  semantics, removing required fields, changing the framing format).
