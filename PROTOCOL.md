# ClipBar sync protocol 2

The tracked macOS daemon and Go Termux companion use this protocol. Version 1
clients are incompatible and must be upgraded and paired. There is no plaintext,
item-first, or older-version fallback. The separate experimental Kotlin client
has not been upgraded to this protocol and is not a supported secure client.

## Transport and identity

Every connection uses **TLS 1.3 with mutual certificate authentication**, normally
on TCP port 2029. Each device owns a P-256 private key and self-signed certificate.
Pairing imports the other device's public certificate after explicit confirmation
of its SHA-256 DER fingerprint through a trusted channel. Both devices must approve
each other. See [pairing instructions](docs/pairing.md).

The TLS certificate chain, validity and client/server usage are verified, and the
exact leaf certificate fingerprint must be in the local trust store. No system CA,
hostname, IP address, Tailscale membership or mDNS advertisement grants trust.
Hostnames are display information; local pairing labels identify approved peers.
Private keys and trust files stay in owner-only local state.

Trust changes reload without restarting. Revocation is checked before each send
and ingestion, and idle revoked streams are closed within about one second.
Previously delivered or in-flight data cannot be recalled. Certificate rotation
requires explicit re-pairing; partial/missing identity files do not silently
regenerate an existing identity.

## Frames and handshake

After TLS, messages are a four-byte unsigned big-endian length followed by UTF-8
JSON. Maximum encoded frame length is 25 MiB. A started frame has bounded header
and body read deadlines; an established idle stream may remain open.

The initiator sends, and the acceptor must answer, exactly this versioned hello:

```json
{"_kind":"hello","version":2,"mode":"stream"}
```

`mode` is either `stream` (persistent bidirectional sync) or `once` (one LAN item
followed by a matching `{"_kind":"ack","content_hash":"..."}` acknowledgement and close). The response echoes the mode. Neither side sends clipboard
content until TLS authentication and the hello exchange have succeeded. Unknown
versions/modes and missing hello frames close the connection.

Persistent connections deduplicate by certificate fingerprint. On simultaneous
crossed dials, the device with the lexicographically smaller fingerprint keeps its
outbound connection and the other keeps its inbound connection.

## Items

| Field | Meaning |
| --- | --- |
| `type` | `text` or `image` |
| `content` | Text, for text items only; maximum 1 MiB UTF-8 |
| `image_data` | Strict base64 of PNG bytes, for images; maximum 16 MiB decoded |
| `content_hash` | Lowercase SHA-256 hex of UTF-8 text or raw PNG bytes |
| `preview` | Optional display hint; receivers derive their own preview |
| `source_mac` | Optional display metadata; never an authorization identity |
| `created_at` | Optional source timestamp; receivers use local receipt time |

Local `id` and `image_path` fields are never sent. Receivers verify content hashes
and types. macOS validates PNG decoding/dimensions (maximum 20 million pixels),
derives an owned image filename, and rejects untrusted paths and symlinks. Termux
supports text clipboard delivery; it ignores image items. Ingested items are not
rebroadcast. Repeated content promotes its existing local row rather than adding
another row; remote timestamps do not control retention ordering.

## Discovery and toggles

LAN discovery uses `_clipd._tcp.local.` with `version=2`, `fingerprint`, and optional
hostname TXT values. These are hints only: the certificate presented by the actual
connection must match the already approved fingerprint. Unapproved advertisers
receive no clipboard payload.

Tailscale discovery polls online peers, or Termux uses a configured `peers.json`.
An existing empty Termux peer list disables discovery. A successful LAN delivery
suppresses a duplicate Tailscale send for that same certificate and item; an
advertisement alone cannot suppress delivery.

The Mac `.tailscale_off` flag disables the Tailscale dial/broadcast paths and
connections arriving through Tailscale address ranges. It does not disable LAN.
Tailscale provides an additional encrypted transport and access controls; it does
not replace ClipBar pairing. The owner-only local peer-status file drives the
native menu; there is no HTTP clipboard service.
