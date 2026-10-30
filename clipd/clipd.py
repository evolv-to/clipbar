"""clipd - clipboard sync daemon with LAN + iCloud fallback."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import stat
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from image_store import (MAX_IMAGE_BYTES, delete_image, image_name, owned_path,
                         read_image, validate_png, write_image)
from private_files import write_private
from pairing import Trust

log = logging.getLogger("clipd")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

CONFIG_DIR = Path(os.environ.get("CLIPBAR_CONFIG_DIR", str(Path.home() / ".config" / "clipd"))).expanduser().absolute()
DB_PATH = CONFIG_DIR / "history.db"
IMAGES_DIR = CONFIG_DIR / "images"
SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"
CAPTURE_SOUND = SOUNDS_DIR / "cassette-record.wav"
SILENCE_FLAG = CONFIG_DIR / ".silent"
TAILSCALE_OFF_FLAG = CONFIG_DIR / ".tailscale_off"
ICLOUD_OFF_FLAG = CONFIG_DIR / ".icloud_off"
SOUND_VOLUME = "0.5"  # afplay -v scale; 0.5 keeps it polite
ICLOUD_DIR = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "clipd"
)


def _icloud_enabled() -> bool:
    """iCloud is a best-effort fallback sync channel. It's on whenever the
    iCloud Drive folder is reachable, unless the user has opted out by
    touching CONFIG_DIR/.icloud_off (mirrors the .tailscale_off switch)."""
    return ICLOUD_DIR.exists() and not ICLOUD_OFF_FLAG.exists()

MAX_ITEMS = 500
POLL_INTERVAL = 0.3
LAN_PORT = int(os.environ.get("CLIPBAR_PORT", "2029"))
BIND_HOST = os.environ.get("CLIPBAR_BIND_HOST", "0.0.0.0")
PROTOCOL_VERSION = 2
HOSTNAME = socket.gethostname()
SERVICE_TYPE = "_clipd._tcp.local."
SERVICE_NAME = f"clipd-{HOSTNAME}.{SERVICE_TYPE}"
MAX_FRAME_BYTES = 25 * 1024 * 1024
# Order matters: prefer the CLI shim over the GUI helper inside the app
# bundle. Under launchd's session, /Applications/Tailscale.app/.../Tailscale
# can fail to talk to tailscaled (no GUI session context), while the
# /usr/local/bin/tailscale and /opt/homebrew/bin/tailscale symlinks talk
# to the daemon directly and work fine for headless agents.
TAILSCALE_BIN_CANDIDATES = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/data/data/com.termux/files/usr/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "tailscale",
)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    if not ICLOUD_OFF_FLAG.exists():
        try:
            ICLOUD_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            type         TEXT    NOT NULL,
            content      TEXT,
            image_path   TEXT,
            preview      TEXT    NOT NULL,
            content_hash TEXT    NOT NULL UNIQUE,
            created_at   REAL    NOT NULL,
            source_mac   TEXT    NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_created ON items(created_at DESC)"
    )
    conn.commit()
    # Quarantine path fields in unsafe legacy rows without opening or deleting
    # their targets. Existing history text remains available.
    for row in conn.execute("SELECT id,type,content_hash,image_path FROM items WHERE image_path IS NOT NULL"):
        try:
            if row["type"] != "image":
                raise ValueError("unexpected image path")
            owned_path(IMAGES_DIR, row["content_hash"], row["image_path"])
        except (ValueError, TypeError):
            conn.execute("UPDATE items SET image_path=NULL WHERE id=?", (row["id"],))
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def prune_items(conn: sqlite3.Connection) -> None:
    pruned = conn.execute("""
        SELECT content_hash,image_path FROM items
        WHERE image_path IS NOT NULL
          AND id NOT IN (
              SELECT id FROM items ORDER BY created_at DESC LIMIT ?
          )
    """, (MAX_ITEMS,)).fetchall()

    conn.execute("""
        DELETE FROM items
        WHERE id NOT IN (
            SELECT id FROM items ORDER BY created_at DESC LIMIT ?
        )
    """, (MAX_ITEMS,))
    conn.commit()

    for content_hash, path in pruned:
        if path:
            try:
                delete_image(IMAGES_DIR, content_hash, path)
            except (OSError, ValueError):
                log.warning("skipped an unowned image while pruning")


def insert_item(
    conn: sqlite3.Connection,
    type_: str,
    content: Optional[str],
    image_path: Optional[str],
    preview: str,
    content_hash: str,
    source_mac: str = HOSTNAME,
) -> Optional[int]:
    try:
        cur = conn.execute("""
            INSERT INTO items
                (type, content, image_path, preview, content_hash, created_at, source_mac)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (type_, content, image_path, preview, content_hash, time.time(), source_mac))
        conn.commit()
        prune_items(conn)
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # Duplicate content (UNIQUE content_hash). Don't silently drop it —
        # re-copying something must bring it back to the TOP of the stream,
        # not leave it buried at its original position. Otherwise the fresh
        # copy looks "missing" from history, and the ⌘V picker highlights a
        # stale row that the swap-with-undo path can paste over the user's
        # text. Re-promote by bumping created_at and return the existing id so
        # the caller treats it like any other capture.
        row = conn.execute(
            "SELECT id FROM items WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE items SET created_at = ? WHERE id = ?",
            (time.time(), row["id"]),
        )
        conn.commit()
        return row["id"]


def promote_item(conn: sqlite3.Connection, item_id: int) -> Optional[dict]:
    conn.execute(
        "UPDATE items SET created_at = ? WHERE id = ?", (time.time(), item_id)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def get_items(
    conn: sqlite3.Connection,
    limit: int = 100,
    offset: int = 0,
    query: str = "",
) -> list[dict]:
    if query:
        rows = conn.execute("""
            SELECT * FROM items
            WHERE type = 'text' AND content LIKE ?
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (f"%{query}%", limit, offset)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM items ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Capture sound
# ---------------------------------------------------------------------------

def play_capture_sound() -> None:
    """Fire-and-forget cassette-record click on each new local capture.
    Skipped if the user toggled off via ClipBar (touches SILENCE_FLAG)."""
    if SILENCE_FLAG.exists():
        return
    if not CAPTURE_SOUND.exists():
        return
    try:
        subprocess.Popen(
            ["afplay", "-v", SOUND_VOLUME, str(CAPTURE_SOUND)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.debug("capture sound failed: %s", exc)


# ---------------------------------------------------------------------------
# Clipboard watcher
# ---------------------------------------------------------------------------

class ClipboardWatcher:
    def __init__(self, conn: sqlite3.Connection, on_new: Callable) -> None:
        self._conn = conn
        self._on_new = on_new
        self._last_count: int = -1
        self._suppress: bool = False

    def suppress_next(self) -> None:
        self._suppress = True

    async def run(self) -> None:
        from AppKit import NSPasteboard  # type: ignore[import]
        pb = NSPasteboard.generalPasteboard()
        while True:
            # The capture loop is the heart of the app — it must never die.
            # Any error in a single iteration (sync, DB lock, decode) is logged
            # and skipped so the next clipboard change is still captured.
            try:
                count = pb.changeCount()
                if count != self._last_count:
                    self._last_count = count
                    suppress_file = CONFIG_DIR / ".suppress"
                    if self._suppress or suppress_file.exists():
                        self._suppress = False
                        suppress_file.unlink(missing_ok=True)
                    else:
                        await self._handle(pb)
            except Exception as exc:
                log.warning("capture iteration failed (loop preserved): %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    async def _handle(self, pb: Any) -> None:
        text = pb.stringForType_("public.utf8-plain-text")
        if text:
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            preview = text[:120].replace("\n", " ").strip()
            item_id = insert_item(
                self._conn, "text", text, None, preview or "[empty]", content_hash
            )
            if item_id:
                play_capture_sound()
                row = dict(
                    self._conn.execute(
                        "SELECT * FROM items WHERE id = ?", (item_id,)
                    ).fetchone()
                )
                log.info("captured clipboard text")
                await self._on_new(row)
            return

        png_data = pb.dataForType_("public.png")
        if png_data is None:
            tiff_data = pb.dataForType_("public.tiff")
            if tiff_data:
                from AppKit import NSBitmapImageRep, NSPNGFileType  # type: ignore[import]
                rep = NSBitmapImageRep.imageRepWithData_(tiff_data)
                if rep:
                    png_data = rep.representationUsingType_properties_(
                        NSPNGFileType, {}
                    )

        if png_data:
            raw = bytes(png_data)
            content_hash = hashlib.sha256(raw).hexdigest()
            img_path = write_image(IMAGES_DIR, content_hash, raw)

            from AppKit import NSImage  # type: ignore[import]
            img = NSImage.alloc().initWithData_(png_data)
            if img:
                sz = img.size()
                preview = f"[image {int(sz.width)}×{int(sz.height)}]"
            else:
                preview = "[image]"

            item_id = insert_item(
                self._conn, "image", None, str(img_path), preview, content_hash
            )
            if item_id:
                play_capture_sound()
                row = dict(
                    self._conn.execute(
                        "SELECT * FROM items WHERE id = ?", (item_id,)
                    ).fetchone()
                )
                row["image_data"] = base64.b64encode(raw).decode()
                log.info("captured clipboard image")
                await self._on_new(row)


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------
# Frames: 4-byte big-endian length prefix + JSON payload.
# Two frame kinds:
#   1. Item frame: a clipboard item dict (has "type", "content_hash", "preview").
#   2. Control frame: dict with "_kind" key. Currently:
#        {"_kind": "hello", "version": 2, "mode": "stream"}
# Hello selects a version and lifecycle; peer identity comes from TLS.
# Stream mode permits reverse pushes over asymmetric tailnet ACLs.
# TLS 1.3 with mutual certificate approval precedes every frame. Both sides
# exchange exact-version hello frames. No plaintext or item-first fallback.

def _frame(payload: dict) -> bytes:
    raw = json.dumps({key: value for key, value in payload.items()
                      if key not in ("image_path", "id")}).encode()
    return struct.pack(">I", len(raw)) + raw


async def _read_frame(
    reader: asyncio.StreamReader, timeout: Optional[float] = 30.0
) -> dict:
    """Read one length-prefixed JSON frame. timeout=None disables it,
    which is what streaming connections want — they may sit idle for
    hours; dead-connection detection is the OS's job (TCP keepalive)."""
    if timeout is None:
        # Idle connections may wait; a started frame must finish promptly.
        first = await reader.readexactly(1)
        size_bytes = first + await asyncio.wait_for(reader.readexactly(3), timeout=10)
    else:
        size_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    size = struct.unpack(">I", size_bytes)[0]
    if size > MAX_FRAME_BYTES:
        raise ValueError(f"frame too large: {size}")
    if timeout is None:
        data = await asyncio.wait_for(reader.readexactly(size), timeout=30)
    else:
        data = await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
    return json.loads(data)


def _norm_host(name: str) -> str:
    """Normalize a hostname for dedup: leading DNS label, lowercased,
    .local stripped. Lets us reconcile mDNS names (e.g. "host.local")
    with tailnet names (e.g. "host") for the same physical device."""
    if not name:
        return ""
    base = name.split(".")[0]
    return base.strip().lower()


def ingest_item(conn: sqlite3.Connection, item: dict, source_label: str) -> bool:
    """Insert a remote item into the local DB. Returns True if new."""
    try:
        if not isinstance(item, dict):
            raise ValueError("invalid item")
        kind = item.get("type")
        identifier = item.get("content_hash")
        image_name(identifier)  # canonical SHA-256 for both supported types
        source = item.get("source_mac", source_label)
        if not isinstance(source, str) or len(source) > 255 or any(ord(c) < 32 for c in source):
            raise ValueError("invalid source")
        source = source_label  # authenticated local pairing label, not a sender claim
        image_path = None
        content = None
        if kind == "text":
            content = item.get("content")
            if not isinstance(content, str) or len(content.encode()) > 1024 * 1024:
                raise ValueError("invalid text")
            if hashlib.sha256(content.encode()).hexdigest() != identifier:
                raise ValueError("text hash mismatch")
            preview = content[:120].replace("\n", " ").strip() or "[empty]"
        elif kind == "image":
            encoded = item.get("image_data")
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                raise ValueError("invalid image data")
            raw = base64.b64decode(encoded, validate=True)
            width, height = validate_png(raw)
            image_path = str(write_image(IMAGES_DIR, identifier, raw))
            preview = f"[image {width}×{height}]"
        else:
            raise ValueError("unsupported item type")
    except (ValueError, TypeError, OSError):
        log.warning("rejected invalid clipboard item")
        return False
    result = insert_item(
        conn, kind, content, image_path, preview, identifier, source,
    )
    if result:
        log.info("received clipboard item from %s", source_label)
        return True
    return False


# ---------------------------------------------------------------------------
# LAN sync
# ---------------------------------------------------------------------------

def _hello(mode: str) -> dict:
    return {"_kind": "hello", "version": PROTOCOL_VERSION, "mode": mode}


def _check_hello(value: Any, mode: str | None = None) -> str:
    if (not isinstance(value, dict) or value.get("_kind") != "hello"
            or type(value.get("version")) is not int or value.get("version") != PROTOCOL_VERSION
            or value.get("mode") not in ("once", "stream")
            or (mode is not None and value["mode"] != mode)):
        raise ValueError("paired protocol version 2 hello required")
    return value["mode"]


def _is_tailnet_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        return (ip in ipaddress.ip_network("100.64.0.0/10") if ip.version == 4
                else ip in ipaddress.ip_network("fd7a:115c:a1e0::/48"))
    except ValueError:
        return False


def _network_allowed(writer) -> bool:
    address = (writer.get_extra_info("peername") or ("",))[0]
    return bool(address) and (not _is_tailnet_address(address) or not TAILSCALE_OFF_FLAG.exists())


class LANSync:
    def __init__(self, conn: sqlite3.Connection, trust: Trust) -> None:
        self._conn = conn
        self.trust = trust
        self.last_sent: set[str] = set()
        self._trust_task = None
        self._server_revision = None
        # service name -> (system_hostname, tailnet_hostname, host, port)
        self._peers: dict[str, tuple[str, str, str, int, str]] = {}
        self._server: Optional[asyncio.Server] = None
        self._tailscale: Optional["TailscaleSync"] = None

    def attach_tailscale(self, ts: "TailscaleSync") -> None:
        self._tailscale = ts

    async def start(self, tailnet_hostname: str = "") -> None:
        from zeroconf import ServiceStateChange  # type: ignore[import]
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf  # type: ignore[import]

        # Bind the sync listener FIRST. Remote peers (LAN broadcast and
        # inbound Tailscale dials) depend on this port; mDNS is only
        # discovery sugar and must not be able to take the listener down.
        self._server = await asyncio.start_server(
            self._handle_peer, BIND_HOST, LAN_PORT,
            ssl=self.trust.context(server=True), ssl_handshake_timeout=5,
            ssl_shutdown_timeout=2,
        )
        self._server_revision = self.trust.revision
        self._trust_task = asyncio.create_task(self._watch_trust())
        log.info("LAN sync on port %d", LAN_PORT)

        if os.environ.get("CLIPBAR_NO_DISCOVERY") == "1":
            return
        self._zc = AsyncZeroconf()
        ip = _local_ip()
        from zeroconf import ServiceInfo  # type: ignore[import]
        # Advertisements are address hints only. The certificate fingerprint
        # must already be approved and must match the actual TLS peer.
        props: dict[str, str] = {"hostname": HOSTNAME, "fingerprint": self.trust.identity, "version": "2"}
        if tailnet_hostname:
            props["tailnet_hostname"] = tailnet_hostname
        info = ServiceInfo(
            SERVICE_TYPE,
            SERVICE_NAME,
            addresses=[socket.inet_aton(ip)],
            port=LAN_PORT,
            properties=props,
        )
        await self._zc.async_register_service(info)

        self._browser = AsyncServiceBrowser(
            self._zc.zeroconf,
            SERVICE_TYPE,
            handlers=[self._on_state_change],
        )

    def _on_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: Any
    ) -> None:
        # Fired from inside zeroconf's event-loop callbacks, so it must not
        # block or call sync APIs (get_service_info raises RuntimeError from
        # the loop). Resolve the peer in a task instead.
        from zeroconf import ServiceStateChange  # type: ignore[import]
        if name == SERVICE_NAME:
            return
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            asyncio.ensure_future(self._resolve_peer(service_type, name))
        elif state_change == ServiceStateChange.Removed:
            self._peers.pop(name, None)

    async def _resolve_peer(self, service_type: str, name: str) -> None:
        from zeroconf.asyncio import AsyncServiceInfo  # type: ignore[import]
        try:
            info = AsyncServiceInfo(service_type, name)
            assert self._zc is not None
            if not await info.async_request(self._zc.zeroconf, 3000):
                return
        except Exception as exc:
            log.debug("LAN peer resolve %s failed: %s", name, exc)
            return
        if not info.addresses:
            return
        host = socket.inet_ntoa(info.addresses[0])
        hostname = ""
        tailnet_hostname = ""
        if info.properties:
            raw_h = info.properties.get(b"hostname")
            if raw_h:
                hostname = raw_h.decode(errors="replace")
            raw_tn = info.properties.get(b"tailnet_hostname")
            if raw_tn:
                tailnet_hostname = raw_tn.decode(errors="replace")
        if not hostname:
            # fall back to parsing the service name "clipd-<host>._..."
            hostname = name.split(".", 1)[0].removeprefix("clipd-")
        identity = (info.properties.get(b"fingerprint") or b"").decode(errors="replace")
        self.trust.reload()
        if identity not in self.trust.peers or info.properties.get(b"version") != b"2":
            self._peers.pop(name, None)
            return
        hostname = self.trust.peers[identity]["name"]
        self._peers[name] = (hostname, tailnet_hostname, host, info.port, identity)
        log.info(
            "LAN peer: %s%s @ %s:%d",
            hostname,
            f"/{tailnet_hostname}" if tailnet_hostname else "",
            host, info.port,
        )

    def peer_hostnames(self) -> set[str]:
        """Normalized leading-label hostnames of currently-discovered LAN
        peers. Returns BOTH the system hostname and tailnet hostname for each
        peer so TailscaleSync can dedup whichever name it knows the peer by."""
        out: set[str] = set()
        for (sysname, tnname, _, _, _) in self._peers.values():
            if sysname:
                out.add(_norm_host(sysname))
            if tnname:
                out.add(_norm_host(tnname))
        return out

    def peer_status(self) -> list[dict]:
        out: list[dict] = []
        for (sysname, tnname, host, port, identity) in self._peers.values():
            if identity not in self.trust.peers:
                continue
            label = sysname or tnname or "?"
            entry = {"hostname": label, "address": f"{host}:{port}", "transport": "lan"}
            if tnname and sysname and _norm_host(tnname) != _norm_host(sysname):
                entry["tailnet_hostname"] = tnname
            out.append(entry)
        return out

    async def broadcast(self, item: dict) -> None:
        self.last_sent = set()
        for _, (_, _, host, port, expected) in list(self._peers.items()):
            writer = None
            try:
                context = self.trust.context(server=False)
                if expected not in self.trust.peers:
                    continue
                reader, writer = await asyncio.wait_for(asyncio.open_connection(
                    host, port, ssl=context, server_hostname="", ssl_handshake_timeout=5,
                    ssl_shutdown_timeout=2), timeout=6)
                identity, _ = self.trust.authorize(writer)
                if identity != expected or not _network_allowed(writer):
                    raise ValueError("unexpected or disabled peer")
                writer.write(_frame(_hello("once")))
                await asyncio.wait_for(writer.drain(), 5)
                _check_hello(await _read_frame(reader, 5), "once")
                self.trust.authorize(writer)
                writer.write(_frame(item))
                await asyncio.wait_for(writer.drain(), 5)
                reply = await _read_frame(reader, 5)
                if not isinstance(reply, dict) or reply.get("_kind") != "ack" or reply.get("content_hash") != item.get("content_hash"):
                    raise ValueError("LAN item was not acknowledged")
                self.last_sent.add(identity)
            except Exception:
                log.debug("paired LAN send failed")
            finally:
                if writer:
                    writer.close()

    async def _handle_peer(self, reader, writer) -> None:
        _enable_keepalive(writer)
        handed_off = False
        try:
            _, label = self.trust.authorize(writer)
            if not _network_allowed(writer):
                raise ValueError("transport disabled")
            mode = _check_hello(await _read_frame(reader, 5))
            writer.write(_frame(_hello(mode)))
            await asyncio.wait_for(writer.drain(), 5)
            if mode == "stream" and self._tailscale is not None:
                self._tailscale.register_inbound(label, reader, writer)
                handed_off = True
                return
            if mode != "once":
                raise ValueError("stream receiver unavailable")
            item = await _read_frame(reader, 10)
            self.trust.authorize(writer)
            if ingest_item(self._conn, item, label):
                writer.write(_frame({"_kind": "ack", "content_hash": item["content_hash"]}))
                await asyncio.wait_for(writer.drain(), 5)
        except (Exception, asyncio.CancelledError):
            pass  # No payload or attacker-controlled metadata in diagnostics.
        finally:
            if not handed_off:
                writer.close()

    async def _watch_trust(self):
        while True:
            try:
                self.trust.reload()
                if self.trust.revision != self._server_revision:
                    if self._server:
                        self._server.close()
                        await self._server.wait_closed()
                    self._server = await asyncio.start_server(
                        self._handle_peer, BIND_HOST, LAN_PORT,
                        ssl=self.trust.context(server=True), ssl_handshake_timeout=5,
                        ssl_shutdown_timeout=2)
                    self._server_revision = self.trust.revision
                    # A newly approved device may already be in the mDNS cache.
                    # Resubscribe so pairing takes effect without a restart.
                    if getattr(self, "_browser", None):
                        from zeroconf.asyncio import AsyncServiceBrowser
                        await self._browser.async_cancel()
                        self._browser = AsyncServiceBrowser(self._zc.zeroconf, SERVICE_TYPE,
                                                            handlers=[self._on_state_change])
                if self._tailscale:
                    self._tailscale.close_unapproved()
            except (OSError, ValueError):
                if self._server:
                    self._server.close()
                    await self._server.wait_closed()
                    self._server = None
                self._server_revision = None
                if self._tailscale:
                    self._tailscale._close_all("invalid trust state")
            await asyncio.sleep(1)

    async def stop(self):
        if self._trust_task:
            self._trust_task.cancel()
            await asyncio.gather(self._trust_task, return_exceptions=True)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if getattr(self, "_browser", None):
            await self._browser.async_cancel()
        if getattr(self, "_zc", None):
            await self._zc.async_close()


# ---------------------------------------------------------------------------
# Tailscale sync
# ---------------------------------------------------------------------------
# Mac↔Mac across the public internet, plus Mac↔Android via the clipa Termux
# agent. Same wire protocol as LAN; the difference is transport (long-lived
# TCP over tailnet IPs) and discovery (`tailscale status --json`).
#
# Connections are persistent + bidirectional. After dial, the dialer sends a
# hello frame so the listener can use the same socket for reverse pushes.
# This is what makes asymmetric ACLs work: if A→B is blocked but B→A allowed,
# B dials and the connection serves both directions.

def _tailscale_binary() -> Optional[str]:
    import shutil
    for cand in TAILSCALE_BIN_CANDIDATES:
        path = shutil.which(cand) if "/" not in cand else (cand if Path(cand).exists() else None)
        if path:
            return path
    return None


class _PeerConn:
    __slots__ = (
        "ip", "hostname", "system_hostname", "reader", "writer",
        "initiated", "address", "since", "fingerprint",
    )

    def __init__(
        self,
        ip: str,
        hostname: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        initiated: bool,
        address: str,
        system_hostname: str = "",
    ) -> None:
        self.fingerprint = ""
        self.ip = ip                      # address only; certificate is identity
        self.hostname = hostname          # display label (tailnet hostname)
        self.system_hostname = system_hostname
        self.reader = reader
        self.writer = writer
        self.initiated = initiated
        self.address = address
        self.since = time.time()

    def names(self) -> set[str]:
        out: set[str] = set()
        if self.hostname:
            out.add(_norm_host(self.hostname))
        if self.system_hostname:
            out.add(_norm_host(self.system_hostname))
        return out

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass


class TailscaleSync:
    def __init__(
        self,
        conn: sqlite3.Connection,
        get_lan_hostnames: Callable[[], set[str]],
        trust: Trust,
    ) -> None:
        self._conn = conn
        self._get_lan_hostnames = get_lan_hostnames  # successfully sent fingerprints
        self.trust = trust
        self._dial_task = None
        # Exact approved certificate fingerprints are identities. Addresses
        # are connection hints; labels come from the local trust store.
        self._connections: dict[str, _PeerConn] = {}
        # ip -> (next_attempt_at, current_delay)
        self._backoff: dict[str, tuple[float, float]] = {}
        self._tailscale_bin: Optional[str] = None
        self._self_hostname: str = HOSTNAME
        self._discovered: dict[str, str] = {}  # hostname -> ip
        self._last_status_at: float = 0.0
        self._enabled = True

    def is_available(self) -> bool:
        return self._tailscale_bin is not None

    def is_enabled(self) -> bool:
        return self._enabled and not TAILSCALE_OFF_FLAG.exists()

    async def start(self) -> None:
        if os.environ.get("CLIPBAR_NO_DISCOVERY") == "1":
            return
        self._tailscale_bin = _tailscale_binary()
        if not self._tailscale_bin:
            log.info("tailscale binary not found; tailscale sync disabled")
            return
        # Discover our tailnet hostname (often differs from socket.gethostname()).
        try:
            status = await self._tailscale_status()
            if status:
                self_obj = status.get("Self") or {}
                ts_host = self_obj.get("HostName")
                if ts_host:
                    self._self_hostname = ts_host
        except Exception:
            pass
        log.info("tailscale sync starting (self=%s)", self._self_hostname)
        self._dial_task = asyncio.create_task(self._dial_loop())

    async def _tailscale_status(self) -> Optional[dict]:
        if not self._tailscale_bin:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._tailscale_bin, "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode != 0 or not stdout:
                return None
            return json.loads(stdout.decode())
        except Exception as exc:
            log.debug("tailscale status failed: %s", exc)
            return None

    async def _refresh_peers(self) -> None:
        status = await self._tailscale_status()
        self._last_status_at = time.time()
        if not status:
            return
        peers: dict[str, str] = {}
        for peer in (status.get("Peer") or {}).values():
            if not peer.get("Online"):
                continue
            hostname = peer.get("HostName") or peer.get("DNSName", "").split(".")[0]
            ips = peer.get("TailscaleIPs") or []
            ipv4 = next((ip for ip in ips if ":" not in ip), None)
            if hostname and ipv4 and hostname != self._self_hostname:
                peers[hostname] = ipv4
        self._discovered = peers

    async def _dial_loop(self) -> None:
        while True:
            try:
                if not self.is_enabled():
                    self.close_unapproved()
                    await asyncio.sleep(5.0)
                    continue
                await self._refresh_peers()
                now = time.time()
                for hostname, ip in self._discovered.items():
                    if any(peer.ip == ip for peer in self._connections.values()):
                        continue  # already connected (in either direction)
                    next_attempt, _ = self._backoff.get(ip, (0.0, 5.0))
                    if now < next_attempt:
                        continue
                    try:
                        await self._dial(hostname, ip)
                        self._backoff.pop(ip, None)
                    except Exception as exc:
                        # Likely ACL block, peer offline, or peer not running
                        # clipd. Back off silently — the peer might still
                        # dial us, and we don't want UI noise.
                        _, prev_delay = self._backoff.get(ip, (0.0, 5.0))
                        delay = min(prev_delay * 2, 120.0)
                        self._backoff[ip] = (now + delay, delay)
                        log.debug("tailscale dial %s@%s failed: %s", hostname, ip, exc)
            except Exception as exc:
                log.warning("tailscale dial loop error: %s", exc)
            await asyncio.sleep(10.0)

    def _close_all(self, reason: str) -> None:
        if not self._connections:
            return
        log.info("tailscale: closing all peers (%s)", reason)
        for conn in list(self._connections.values()):
            conn.close()
        self._connections.clear()

    async def _dial(self, hostname: str, ip: str) -> None:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            ip, LAN_PORT, ssl=self.trust.context(server=False), server_hostname="",
            ssl_handshake_timeout=5, ssl_shutdown_timeout=2), timeout=6)
        try:
            identity, label = self.trust.authorize(writer)
            if not self.is_enabled() or not _network_allowed(writer):
                raise ValueError("transport disabled")
            _enable_keepalive(writer)
            writer.write(_frame(_hello("stream")))
            await asyncio.wait_for(writer.drain(), timeout=5)
            _check_hello(await _read_frame(reader, 5), "stream")
            peer = _PeerConn(ip, label, reader, writer, True, ip)
            peer.fingerprint = identity
            self._adopt(peer)
        except BaseException:
            writer.close()
            raise

    def register_inbound(self, hostname, reader, writer, system_hostname="") -> None:
        identity, label = self.trust.authorize(writer)
        address = writer.get_extra_info("peername") or ("", 0)
        peer = _PeerConn(address[0], label, reader, writer, False, str(address[0]))
        peer.fingerprint = identity
        self._adopt(peer)

    def _adopt(self, peer: _PeerConn) -> bool:
        existing = self._connections.get(peer.fingerprint)
        if existing:
            # Both sides make the same decision when simultaneous dials cross.
            prefer_outbound = self.trust.identity < peer.fingerprint
            if peer.initiated != prefer_outbound or existing.initiated == prefer_outbound:
                peer.close()
                return False
            existing.close()
        self._connections[peer.fingerprint] = peer
        asyncio.create_task(self._read_loop(peer))
        return True

    def close_unapproved(self):
        for peer in list(self._connections.values()):
            try:
                self.trust.authorize(peer.writer)
                if not _network_allowed(peer.writer):
                    raise ValueError("transport disabled")
            except ValueError:
                peer.close()

    async def stop(self):
        if self._dial_task:
            self._dial_task.cancel()
            await asyncio.gather(self._dial_task, return_exceptions=True)
        self._close_all("shutdown")

    async def _read_loop(self, peer: _PeerConn) -> None:
        try:
            while True:
                # Streaming reads have no per-frame deadline. Long-lived
                # connections may sit idle for hours; the kernel's TCP
                # keepalive (SO_KEEPALIVE, set in _enable_keepalive) is
                # what detects a dead peer.
                item = await _read_frame(peer.reader, timeout=None)
                if isinstance(item, dict) and item.get("_kind"):
                    continue  # ignore unknown control frames
                self.trust.authorize(peer.writer)
                if not _network_allowed(peer.writer):
                    raise ValueError("transport disabled")
                ingest_item(self._conn, item, peer.hostname)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        except Exception as exc:
            log.debug("tailscale read %s error: %s", peer.hostname, exc)
        finally:
            peer.close()
            if self._connections.get(peer.fingerprint) is peer:
                self._connections.pop(peer.fingerprint, None)
                log.info("tailscale: disconnected from %s", peer.hostname)

    async def broadcast(self, item: dict) -> None:
        if not self._connections:
            return
        lan = self._get_lan_hostnames()
        framed = _frame(item)
        for ip, peer in list(self._connections.items()):
            if peer.fingerprint in lan:
                log.debug("tailscale: skip %s (reachable on LAN)", peer.hostname)
                continue
            try:
                self.trust.authorize(peer.writer)
                if not _network_allowed(peer.writer):
                    raise ValueError("transport disabled")
                peer.writer.write(framed)
                await asyncio.wait_for(peer.writer.drain(), timeout=10.0)
                log.debug("tailscale: sent %d bytes to %s", len(framed), peer.hostname)
            except Exception as exc:
                log.info("tailscale send to %s failed (closing): %s", peer.hostname, exc)
                peer.close()
                if self._connections.get(peer.fingerprint) is peer:
                    self._connections.pop(peer.fingerprint, None)

    def peer_status(self) -> list[dict]:
        out: list[dict] = []
        for peer in self._connections.values():
            entry = {
                "hostname": peer.hostname,
                "address": peer.address,
                "transport": "tailscale" if _is_tailnet_address(peer.ip) else "lan",
                "direction": "out" if peer.initiated else "in",
                "since": peer.since,
            }
            if peer.system_hostname and _norm_host(peer.system_hostname) != _norm_host(peer.hostname):
                entry["system_hostname"] = peer.system_hostname
            out.append(entry)
        return out

    @property
    def self_hostname(self) -> str:
        return self._self_hostname

    def discovered_status(self) -> list[dict]:
        return [
            {"hostname": h, "address": ip, "connected": any(p.ip == ip for p in self._connections.values())}
            for h, ip in self._discovered.items()
        ]


# ---------------------------------------------------------------------------
# iCloud fallback
# ---------------------------------------------------------------------------

class ICloudSync:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._seen: dict[str, float] = {}

    async def write(self, item: dict) -> None:
        if not _icloud_enabled():
            return
        out = {key: value for key, value in item.items() if key not in ("image_path", "id")}
        if item.get("type") == "image":
            try:
                out["image_data"] = base64.b64encode(
                    read_image(IMAGES_DIR, item["content_hash"], item.get("image_path"))
                ).decode()
            except (OSError, ValueError, KeyError):
                log.warning("skipped unowned image during cloud export")
                return
        path = ICLOUD_DIR / f"{HOSTNAME}-latest.json"
        try:
            write_private(path, json.dumps(out).encode())
        except OSError as exc:
            # The iCloud Drive file provider can transiently fail (EDEADLK
            # "resource deadlock avoided", eviction, sync locks). Swallow it —
            # a sync hiccup must never crash capture.
            log.debug("icloud write failed: %s", exc)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(3.0)
            # Self-healing: a bad scan iteration is logged and skipped, never
            # raised — this runs alongside the capture loop under one gather,
            # so an unhandled error here would tear capture down too.
            try:
                if not _icloud_enabled():
                    continue
                for f in ICLOUD_DIR.glob("*-latest.json"):
                    stem = f.stem.replace("-latest", "")
                    if stem == HOSTNAME:
                        continue
                    try:
                        mtime = f.stat().st_mtime
                    except OSError:
                        continue
                    if self._seen.get(str(f)) == mtime:
                        continue
                    self._seen[str(f)] = mtime
                    await self._ingest(f)
            except Exception as exc:
                log.debug("icloud watch iteration failed: %s", exc)

    async def _ingest(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("invalid snapshot file")
                data = stream.read(MAX_FRAME_BYTES + 1)
            if len(data) > MAX_FRAME_BYTES:
                raise ValueError("oversized snapshot")
            item = json.loads(data)
        except Exception as exc:
            log.debug("icloud ingest error %s: %s", path, exc)
            return

        ingest_item(self._conn, item, "iCloud")


# ---------------------------------------------------------------------------
# Native peer status (local owner-only file; no HTTP clipboard API)
# ---------------------------------------------------------------------------

def write_peer_status(lan: "LANSync", tailscale: "TailscaleSync") -> None:
    streams = tailscale.peer_status()
    snapshot = {
        "self": HOSTNAME,
        "lan": lan.peer_status() + [peer for peer in streams if peer.get("transport") == "lan"],
        "tailscale": [peer for peer in streams if peer.get("transport") != "lan"],
        "tailscale_discovered": tailscale.discovered_status(),
        "tailscale_available": tailscale.is_available(),
        "tailscale_enabled": tailscale.is_enabled(),
    }
    write_private(CONFIG_DIR / "peer-status.json", json.dumps(snapshot).encode())


async def publish_peer_status(lan: "LANSync", tailscale: "TailscaleSync") -> None:
    try:
        while True:
            try:
                write_peer_status(lan, tailscale)
            except (OSError, ValueError):
                log.warning("could not update local peer status")
            await asyncio.sleep(2.0)
    finally:
        # A stopped daemon must not leave a connected status behind. The native
        # reader also rejects snapshots older than ten seconds after a crash.
        try:
            (CONFIG_DIR / "peer-status.json").unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
    """Turn on TCP keepalive so the kernel detects dead peers in the
    background. Defaults are platform-specific (~2h on Linux/macOS); good
    enough — we just need the connection to *eventually* drop instead of
    silently wedging."""
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    setup_dirs()
    conn = setup_db()

    trust = Trust(CONFIG_DIR)
    lan = LANSync(conn, trust)
    tailscale = TailscaleSync(conn, lambda: lan.last_sent, trust)
    lan.attach_tailscale(tailscale)
    icloud = ICloudSync(conn)

    async def on_new(item: dict) -> None:
        # Each sync sink is independent and best-effort: a failure in one
        # (e.g. a transient iCloud Drive file lock, an unreachable peer) must
        # never propagate up and kill the capture loop, nor block the others.
        payload = {key: value for key, value in item.items() if key not in ("image_path", "id")}
        for name, sink in (
            ("lan", lan.broadcast),
            ("tailscale", tailscale.broadcast),
            ("icloud", icloud.write),
        ):
            try:
                await sink(item if name == "icloud" else payload)
            except Exception as exc:
                log.warning("%s sync failed (capture preserved): %s", name, exc)

    watcher = ClipboardWatcher(conn, on_new)

    try:
        await tailscale.start()
    except Exception as exc:
        log.warning("tailscale sync disabled: %s", exc)

    try:
        # tailscale.start() resolved our tailnet hostname (if available) so
        # LAN's mDNS advertisement can include it for cross-channel dedup.
        tn = tailscale.self_hostname if tailscale.is_available() else ""
        await lan.start(tailnet_hostname=tn if _norm_host(tn) != _norm_host(HOSTNAME) else "")
    except Exception as exc:
        log.warning("LAN sync disabled: %s", exc)

    try:
        await asyncio.gather(watcher.run(), icloud.watch(), publish_peer_status(lan, tailscale))
    finally:
        await lan.stop()
        await tailscale.stop()
        conn.close()


def cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="ClipBar clipboard daemon and device pairing")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the daemon")
    identity = sub.add_parser("identity", help="show this device's public certificate fingerprint")
    identity.add_argument("--export", type=Path, help="export the public certificate (never the private key)")
    pair = sub.add_parser("pair", help="approve a device after confirming its fingerprint")
    pair.add_argument("--certificate", type=Path, required=True)
    pair.add_argument("--fingerprint", required=True)
    pair.add_argument("--name", required=True)
    unpair = sub.add_parser("unpair", help="revoke a paired device")
    unpair.add_argument("fingerprint")
    sub.add_parser("paired", help="list locally approved device labels and fingerprints")
    args = parser.parse_args()
    if args.command in (None, "run"):
        asyncio.run(main())
        return
    from private_files import read_private
    try:
        trust = Trust(CONFIG_DIR)
        if args.command == "identity":
            print(trust.identity)
            if args.export:
                write_private(args.export.absolute(), read_private(trust.cert))
        elif args.command == "pair":
            with args.certificate.open("rb") as stream:
                certificate = stream.read(16385).decode()
            trust.pair(certificate, args.fingerprint, args.name)
            print("Peer paired. Confirm this device on the other peer too.")
        elif args.command == "unpair":
            trust.unpair(args.fingerprint)
            print("Peer removed. Active connections close within one second.")
        elif args.command == "paired":
            for identity, peer in trust.peers.items():
                print(identity, peer["name"])
    except (OSError, ValueError):
        parser.exit(1, "Pairing failed. Check certificate, fingerprint, and private state permissions.\n")


if __name__ == "__main__":
    cli()
