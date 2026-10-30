"""clipd - clipboard sync daemon with LAN + iCloud fallback."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import socket
import sqlite3
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("clipd")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

CONFIG_DIR = Path.home() / ".config" / "clipd"
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
HTTP_PORT = 2028
LAN_PORT = 2029
PROTOCOL_VERSION = 1
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
    return conn


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def prune_items(conn: sqlite3.Connection) -> None:
    pruned = conn.execute("""
        SELECT image_path FROM items
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

    for (path,) in pruned:
        if path:
            Path(path).unlink(missing_ok=True)


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
            img_path = IMAGES_DIR / f"{content_hash}.png"
            if not img_path.exists():
                img_path.write_bytes(raw)

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
#        {"_kind": "hello", "hostname": "...", "version": 1}
# Hello must be the first frame on a long-lived connection. It tells the
# receiver who's on the other end, which lets the receiver use this same
# socket for outbound items (critical for asymmetric tailnet ACLs where only
# one side can dial).
# A connection without a hello is treated as a one-shot LAN broadcast: read
# frames until EOF, ingest each.

def _frame(payload: dict) -> bytes:
    raw = json.dumps(payload).encode()
    return struct.pack(">I", len(raw)) + raw


async def _read_frame(
    reader: asyncio.StreamReader, timeout: Optional[float] = 30.0
) -> dict:
    """Read one length-prefixed JSON frame. timeout=None disables it,
    which is what streaming connections want — they may sit idle for
    hours; dead-connection detection is the OS's job (TCP keepalive)."""
    if timeout is None:
        size_bytes = await reader.readexactly(4)
    else:
        size_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    size = struct.unpack(">I", size_bytes)[0]
    if size > MAX_FRAME_BYTES:
        raise ValueError(f"frame too large: {size}")
    if timeout is None:
        data = await reader.readexactly(size)
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
    if item.get("type") == "image" and "image_data" in item:
        try:
            raw = base64.b64decode(item.pop("image_data"))
            img_path = IMAGES_DIR / f"{item['content_hash']}.png"
            img_path.write_bytes(raw)
            item["image_path"] = str(img_path)
        except Exception as exc:
            log.debug("image ingest failed: %s", exc)

    result = insert_item(
        conn,
        item["type"],
        item.get("content"),
        item.get("image_path"),
        item["preview"],
        item["content_hash"],
        item.get("source_mac", source_label),
    )
    if result:
        log.info("received clipboard item from %s", source_label)
        return True
    return False


# ---------------------------------------------------------------------------
# LAN sync
# ---------------------------------------------------------------------------

class LANSync:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # service name -> (system_hostname, tailnet_hostname, host, port)
        self._peers: dict[str, tuple[str, str, str, int]] = {}
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
            self._handle_peer, "0.0.0.0", LAN_PORT
        )
        log.info("LAN sync on port %d", LAN_PORT)

        self._zc = AsyncZeroconf()
        ip = _local_ip()
        from zeroconf import ServiceInfo  # type: ignore[import]
        # Advertise BOTH identities so cross-channel dedup works even when
        # the system hostname (Bonjour-style "host.local") differs from
        # the tailnet name (whatever Tailscale assigned the device). The
        # Tailscale hello frame carries the same pair.
        props: dict[str, str] = {"hostname": HOSTNAME}
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
        self._peers[name] = (hostname, tailnet_hostname, host, info.port)
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
        for (sysname, tnname, _, _) in self._peers.values():
            if sysname:
                out.add(_norm_host(sysname))
            if tnname:
                out.add(_norm_host(tnname))
        return out

    def peer_status(self) -> list[dict]:
        out: list[dict] = []
        for (sysname, tnname, host, port) in self._peers.values():
            label = sysname or tnname or "?"
            entry = {"hostname": label, "address": f"{host}:{port}", "transport": "lan"}
            if tnname and sysname and _norm_host(tnname) != _norm_host(sysname):
                entry["tailnet_hostname"] = tnname
            out.append(entry)
        return out

    async def broadcast(self, item: dict) -> None:
        framed = _frame(item)
        for name, (hostname, _tn, host, port) in list(self._peers.items()):
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=2.0
                )
                writer.write(framed)
                await asyncio.wait_for(writer.drain(), timeout=2.0)
                writer.close()
            except Exception as exc:
                log.debug("LAN broadcast to %s failed: %s", hostname, exc)

    async def _handle_peer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        _enable_keepalive(writer)
        peer_label = "lan-peer"
        handed_off = False
        try:
            first = await _read_frame(reader, timeout=10.0)
            if isinstance(first, dict) and first.get("_kind") == "hello":
                hostname = str(first.get("hostname") or "tailscale-peer")
                system_hostname = str(first.get("system_hostname") or "")
                if self._tailscale is not None:
                    self._tailscale.register_inbound(
                        hostname, reader, writer, system_hostname=system_hostname,
                    )
                    handed_off = True
                    return
                peer_label = hostname
            else:
                ingest_item(self._conn, first, peer_label)
                # Some peers may keep the connection open and stream more
                # items. Keep reading until EOF — no per-read deadline,
                # idle is fine (TCP keepalive detects dead connections).
                while True:
                    item = await _read_frame(reader, timeout=None)
                    ingest_item(self._conn, item, peer_label)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        except Exception as exc:
            log.debug("peer receive error: %s", exc)
        finally:
            if not handed_off:
                try:
                    writer.close()
                except Exception:
                    pass


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
        "initiated", "address", "since",
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
        self.ip = ip                      # tailnet IP — the dedup key
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
    ) -> None:
        self._conn = conn
        self._get_lan_hostnames = get_lan_hostnames
        # key = tailnet IP. Hostnames are display-only because each side
        # may know the peer by a different name (mDNS hostname, tailnet
        # hostname, --hostname override). The IP is the unambiguous identity.
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
        asyncio.create_task(self._dial_loop())

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
                    self._close_all("disabled")
                    await asyncio.sleep(5.0)
                    continue
                await self._refresh_peers()
                now = time.time()
                for hostname, ip in self._discovered.items():
                    if ip in self._connections:
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
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, LAN_PORT), timeout=5.0
        )
        _enable_keepalive(writer)
        hello = {
            "_kind": "hello",
            "hostname": self._self_hostname,    # tailnet name
            "system_hostname": HOSTNAME,        # OS hostname (for cross-channel dedup)
            "version": PROTOCOL_VERSION,
        }
        writer.write(_frame(hello))
        await asyncio.wait_for(writer.drain(), timeout=5.0)
        peer = _PeerConn(
            ip=ip, hostname=hostname,
            reader=reader, writer=writer,
            initiated=True, address=ip,
        )
        if not self._adopt(peer):
            log.debug("tailscale: dial to %s already covered, dropping", ip)
            return
        log.info("tailscale: connected to %s @ %s", hostname, ip)

    def register_inbound(
        self,
        hostname: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        system_hostname: str = "",
    ) -> None:
        try:
            peername = writer.get_extra_info("peername") or ("?", 0)
            ip = peername[0]
            address = f"{peername[0]}:{peername[1]}"
        except Exception:
            ip = ""
            address = "?"
        peer = _PeerConn(
            ip=ip, hostname=hostname,
            reader=reader, writer=writer,
            initiated=False, address=address,
            system_hostname=system_hostname,
        )
        if not self._adopt(peer):
            log.debug("tailscale: inbound from %s already covered, dropping", ip)
            return
        log.info(
            "tailscale: accepted from %s%s @ %s",
            hostname,
            f"/{system_hostname}" if system_hostname and _norm_host(system_hostname) != _norm_host(hostname) else "",
            address,
        )

    def _adopt(self, peer: _PeerConn) -> bool:
        """Add peer to the registry. Returns True if adopted, False if a
        live connection for the same IP already exists (the new one is
        rejected to avoid duplicate sends to the same physical device)."""
        if not peer.ip:
            # No IP we can dedup against — best-effort: just accept.
            asyncio.create_task(self._read_loop(peer))
            return True
        existing = self._connections.get(peer.ip)
        if existing is not None:
            # Already connected to this peer in some direction. Refuse the
            # new connection. The old one will be cleaned up by its own
            # read loop on EOF / failed write.
            peer.close()
            return False
        self._connections[peer.ip] = peer
        asyncio.create_task(self._read_loop(peer))
        return True

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
                ingest_item(self._conn, item, f"tailscale:{peer.hostname}")
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        except Exception as exc:
            log.debug("tailscale read %s error: %s", peer.hostname, exc)
        finally:
            peer.close()
            if peer.ip and self._connections.get(peer.ip) is peer:
                self._connections.pop(peer.ip, None)
                log.info("tailscale: disconnected from %s", peer.hostname)

    async def broadcast(self, item: dict) -> None:
        if not self.is_enabled() or not self._connections:
            return
        lan = self._get_lan_hostnames()
        framed = _frame(item)
        for ip, peer in list(self._connections.items()):
            if peer.names() & lan:
                log.debug("tailscale: skip %s (reachable on LAN)", peer.hostname)
                continue
            try:
                peer.writer.write(framed)
                await asyncio.wait_for(peer.writer.drain(), timeout=10.0)
                log.debug("tailscale: sent %d bytes to %s", len(framed), peer.hostname)
            except Exception as exc:
                log.info("tailscale send to %s failed (closing): %s", peer.hostname, exc)
                peer.close()
                if self._connections.get(ip) is peer:
                    self._connections.pop(ip, None)

    def peer_status(self) -> list[dict]:
        out: list[dict] = []
        for peer in self._connections.values():
            entry = {
                "hostname": peer.hostname,
                "address": peer.address,
                "transport": "tailscale",
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
            {"hostname": h, "address": ip, "connected": ip in self._connections}
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
        out = dict(item)
        if out.get("image_path") and "image_data" not in out:
            try:
                out["image_data"] = base64.b64encode(
                    Path(out["image_path"]).read_bytes()
                ).decode()
            except Exception:
                pass
        path = ICLOUD_DIR / f"{HOSTNAME}-latest.json"
        try:
            path.write_text(json.dumps(out))
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
            item = json.loads(path.read_text())
        except Exception as exc:
            log.debug("icloud ingest error %s: %s", path, exc)
            return

        if item.get("type") == "image" and "image_data" in item:
            raw = base64.b64decode(item.pop("image_data"))
            img_path = IMAGES_DIR / f"{item['content_hash']}.png"
            img_path.write_bytes(raw)
            item["image_path"] = str(img_path)

        result = insert_item(
            self._conn,
            item["type"],
            item.get("content"),
            item.get("image_path"),
            item["preview"],
            item["content_hash"],
            item.get("source_mac", "icloud"),
        )
        if result:
            log.info("received clipboard item from iCloud")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

PANEL_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>ClipBar</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#1c1c1e;--surface:#2c2c2e;--hover:#3a3a3c;--border:#3a3a3c;
  --text:#f2f2f7;--muted:#8e8e93;--accent:#0a84ff;--r:10px;
}
@media(prefers-color-scheme:light){
  :root{--bg:#f2f2f7;--surface:#fff;--hover:#e5e5ea;--border:#d1d1d6;
        --text:#1c1c1e;--muted:#8e8e93;--accent:#007aff;}
}
html,body{height:100%;overflow:hidden}
body{
  background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
  font-size:13px;display:flex;flex-direction:column;
}
.top{padding:10px 12px 0;display:flex;align-items:center;justify-content:space-between}
.top h1{font-size:12px;font-weight:600;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.top .cnt{font-size:11px;color:var(--muted)}
.search{padding:8px 10px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.search svg{color:var(--muted);flex-shrink:0}
.search input{background:transparent;border:none;outline:none;color:var(--text);font-size:13px;width:100%;font-family:inherit}
.search input::placeholder{color:var(--muted)}
.list{flex:1;overflow-y:auto;padding:6px}
.list::-webkit-scrollbar{width:4px}
.list::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.item{
  display:flex;align-items:center;gap:10px;padding:8px 10px;
  border-radius:var(--r);cursor:pointer;transition:background .08s;margin-bottom:2px;min-height:52px;
}
.item:hover,.item.sel{background:var(--hover)}
.item.sel{outline:1px solid var(--accent)55;background:color-mix(in srgb,var(--accent) 12%,var(--hover))}
.thumb{width:38px;height:38px;border-radius:6px;object-fit:cover;flex-shrink:0;background:var(--border)}
.icon{
  width:38px;height:38px;border-radius:6px;background:var(--surface);
  border:1px solid var(--border);display:flex;align-items:center;justify-content:center;
  flex-shrink:0;font-size:17px;
}
.body{flex:1;min-width:0}
.preview{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.45}
.preview.mono{font-family:'SF Mono',Menlo,monospace;font-size:12px}
.meta{color:var(--muted);font-size:11px;margin-top:2px;display:flex;gap:6px}
.empty{text-align:center;color:var(--muted);padding:48px 20px;font-size:13px}
.close-btn{
  background:none;border:none;cursor:pointer;color:var(--muted);padding:2px 4px;
  border-radius:4px;font-size:15px;line-height:1;transition:color .1s;
}
.close-btn:hover{color:var(--text)}
</style>
</head>
<body>
<div class="top"><h1>Clipboard</h1><div style="display:flex;align-items:center;gap:8px"><span class="cnt" id="cnt"></span><button class="close-btn" onclick="closePanel()" title="Close (Esc)">✕</button></div></div>
<div class="search">
  <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
    <path d="M11.742 10.344a6.5 6.5 0 1 0-1.397 1.398h-.001l3.85 3.85a1 1 0 0 0 1.415-1.414l-3.85-3.85a1.007 1.007 0 0 0-.115-.1zM12 6.5a5.5 5.5 0 1 1-11 0 5.5 5.5 0 0 1 11 0z"/>
  </svg>
  <input id="q" type="search" placeholder="Search…" autocomplete="off">
</div>
<div class="list" id="list"></div>
<script>
let items=[], sel=0;
const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function ago(ts){
  const d=Date.now()/1e3-ts;
  if(d<60)return Math.floor(d)+'s';
  if(d<3600)return Math.floor(d/60)+'m';
  if(d<86400)return Math.floor(d/3600)+'h';
  return Math.floor(d/86400)+'d';
}
function closePanel(){try{window.webkit.messageHandlers.close.postMessage(null);}catch{}}
async function load(q=''){
  try{
    const r=await fetch(q?`/items?q=${encodeURIComponent(q)}`:'/items');
    items=await r.json();
    render();
  }catch{
    document.getElementById('list').innerHTML='<div class="empty">Starting daemon…</div>';
    setTimeout(()=>load(q),1500);
  }
}
function render(){
  document.getElementById('cnt').textContent=items.length+' items';
  const L=document.getElementById('list');
  if(!items.length){L.innerHTML='<div class="empty">Nothing copied yet</div>';return;}
  L.innerHTML=items.map((it,i)=>{
    const isImg=it.type==='image';
    const thumb=isImg
      ?`<img class="thumb" src="/images/${it.content_hash}.png" loading="lazy">`
      :`<div class="icon">📋</div>`;
    const src=it.source_mac&&it.source_mac!==location.hostname?`<span>📡 ${esc(it.source_mac)}</span>`:'';
    return`<div class="item${i===sel?' sel':''}" data-id="${it.id}" data-i="${i}">
      ${thumb}
      <div class="body">
        <div class="preview${isImg?'':' mono'}">${esc(it.preview)}</div>
        <div class="meta"><span>${ago(it.created_at)}</span>${src}</div>
      </div>
    </div>`;
  }).join('');
}
async function pick(id){
  await fetch('/promote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  try{window.webkit.messageHandlers.paste.postMessage(id);}catch{}
}
document.getElementById('list').addEventListener('click',e=>{
  const el=e.target.closest('.item');
  if(el)pick(+el.dataset.id);
});
document.addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'){sel=Math.min(sel+1,items.length-1);render();e.preventDefault();}
  else if(e.key==='ArrowUp'){sel=Math.max(sel-1,0);render();e.preventDefault();}
  else if(e.key==='Enter'&&items[sel]){pick(items[sel].id);}
  else if(e.key==='Escape'){try{window.webkit.messageHandlers.close.postMessage(null);}catch{}}
});
document.getElementById('q').addEventListener('input',e=>{sel=0;load(e.target.value);});
setInterval(()=>{if(!document.getElementById('q').value)load();},2500);
load();
document.getElementById('q').focus();
</script>
</body>
</html>"""


def make_app(
    conn: sqlite3.Connection,
    watcher: ClipboardWatcher,
    lan: Optional["LANSync"] = None,
    tailscale: Optional["TailscaleSync"] = None,
) -> Any:
    from aiohttp import web  # type: ignore[import]

    async def index(_: web.Request) -> web.Response:
        return web.Response(text=PANEL_HTML, content_type="text/html")

    async def items_handler(req: web.Request) -> web.Response:
        q = req.query.get("q", "")
        return web.json_response(get_items(conn, query=q))

    async def image_handler(req: web.Request) -> web.Response:
        p = IMAGES_DIR / req.match_info["filename"]
        if not p.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(p)

    async def promote_handler(req: web.Request) -> web.Response:
        body = await req.json()
        item_id = int(body.get("id", 0))
        if not item_id:
            raise web.HTTPBadRequest()
        item = promote_item(conn, item_id)
        if not item:
            raise web.HTTPNotFound()

        watcher.suppress_next()
        from AppKit import NSData, NSPasteboard  # type: ignore[import]
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        if item["type"] == "text":
            pb.setString_forType_(item["content"], "public.utf8-plain-text")
        else:
            raw = Path(item["image_path"]).read_bytes()
            ns_data = NSData.dataWithBytes_length_(raw, len(raw))
            pb.setData_forType_(ns_data, "public.png")

        return web.json_response({"ok": True})

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "hostname": HOSTNAME})

    async def peers_handler(_: web.Request) -> web.Response:
        lan_peers = lan.peer_status() if lan else []
        ts_peers = tailscale.peer_status() if tailscale else []
        ts_discovered = tailscale.discovered_status() if tailscale else []
        return web.json_response({
            "self": HOSTNAME,
            "lan": lan_peers,
            "tailscale": ts_peers,
            "tailscale_discovered": ts_discovered,
            "tailscale_available": bool(tailscale and tailscale.is_available()),
            "tailscale_enabled": bool(tailscale and tailscale.is_enabled()),
        })

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/items", items_handler)
    app.router.add_get("/images/{filename}", image_handler)
    app.router.add_post("/promote", promote_handler)
    app.router.add_get("/health", health)
    app.router.add_get("/peers", peers_handler)
    return app


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

    lan = LANSync(conn)
    tailscale = TailscaleSync(conn, lan.peer_hostnames)
    lan.attach_tailscale(tailscale)
    icloud = ICloudSync(conn)

    async def on_new(item: dict) -> None:
        # Each sync sink is independent and best-effort: a failure in one
        # (e.g. a transient iCloud Drive file lock, an unreachable peer) must
        # never propagate up and kill the capture loop, nor block the others.
        payload = dict(item)
        for name, sink in (
            ("lan", lan.broadcast),
            ("tailscale", tailscale.broadcast),
            ("icloud", icloud.write),
        ):
            try:
                await sink(payload)
            except Exception as exc:
                log.warning("%s sync failed (capture preserved): %s", name, exc)

    watcher = ClipboardWatcher(conn, on_new)

    from aiohttp import web  # type: ignore[import]
    runner = web.AppRunner(make_app(conn, watcher, lan, tailscale))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", HTTP_PORT).start()
    log.info("HTTP on http://127.0.0.1:%d", HTTP_PORT)

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

    await asyncio.gather(watcher.run(), icloud.watch())


if __name__ == "__main__":
    asyncio.run(main())
