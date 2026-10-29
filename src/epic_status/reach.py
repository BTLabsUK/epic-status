"""Connectivity self-test for wallets: `GET /api/v1/ip` and `GET /api/v1/reach`.

A wallet that wants to receive directly needs two answers it cannot get from inside its own
network: which public address the internet sees, and whether a port it listens on is reachable
from outside. Both used to come from third parties; these routes give them from here.

The one rule that makes `reach` safe to publish: **it only ever connects back to the address
the request came from.** There is no target parameter, so it cannot be aimed at anyone else.
The address is nginx's `$remote_addr` (`x-real-ip`, which the vhost sets and a client cannot),
never a header the client wrote. On top of that:

- ports 1024–65535 only, so no well-known service is ever touched;
- this server's own addresses are refused, so it cannot be turned on itself;
- per visitor (IPv4 address or IPv6 /64, salted hash in memory) 10 a minute and 60 an hour,
  and at most 8 at once in total; nginx holds the same per-address rate in front;
- one TCP connect with a 5 s timeout, and when it opens, one JSON-RPC `check_version` to the
  Epic wallet's foreign API (`POST /v2/foreign`, plain HTTP) on that same connection, with
  its reply capped and read within the same 5 s.

Nothing here logs an address. Over the onion every request comes from 127.0.0.1, so both
routes answer that they are not available over Tor rather than echo the onion's own side.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections.abc import Callable

from .net import _embedded_ipv4, _is_public
from .quickcheck import LocalAddresses, QuickCheckLimiter, _canonical, _Window

PORT_MIN = 1024
PORT_MAX = 65535
TIMEOUT_S = 5.0
PER_MINUTE = 10
PER_HOUR = 60
MAX_CONCURRENT = 8
BUSY_RETRY_S = 5
MAX_REPLY_BYTES = 16 * 1024
FOREIGN_API_PATH = "/v2/foreign"

TOR_ERROR = "not available over Tor"
PORT_ERROR = f"port must be an integer from {PORT_MIN} to {PORT_MAX}"

_CHECK_VERSION = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "check_version", "params": []}, separators=(",", ":")
).encode()


class ReachLimiter(QuickCheckLimiter):
    """The quick check's per-visitor keying, with this route's own allowances."""

    def __init__(self, salt: str, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(salt, clock)
        self._minute = _Window(PER_MINUTE, 60.0)
        self._hour = _Window(PER_HOUR, 3600.0)

    def acquire(self) -> bool:
        if self.active >= MAX_CONCURRENT:
            return False
        self.active += 1
        return True


class ReachError(Exception):
    """A refusal: the HTTP status, the message, and a retry delay for 429s."""

    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


def parse_port(raw: str | None) -> int:
    """Digits only, in range. `int()` alone would take "+2000", " 2000" and "2_000"."""
    if raw is None or not raw.isascii() or not raw.isdigit() or len(raw) > 5:
        raise ReachError(400, PORT_ERROR)
    port = int(raw)
    if not PORT_MIN <= port <= PORT_MAX:
        raise ReachError(400, PORT_ERROR)
    return port


def public_address(peer: str, *, onion: bool) -> str:
    """The requester's address as this server will answer it, or a refusal.

    `peer` is the proxy's statement of who connected (see `api._peer`). Over the onion it
    is always loopback; anything that is not a public address is refused the same way,
    so the onion's or the proxy's own side is never echoed back as someone's address.
    """
    if onion:
        raise ReachError(400, TOR_ERROR)
    try:
        ip = ipaddress.ip_address(peer.split("%", 1)[0])
    except ValueError:
        raise ReachError(400, "no public address was seen for this request") from None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not _is_public(ip):
        raise ReachError(400, "no public address was seen for this request")
    return str(ip)


def _own(ip: str, local: LocalAddresses) -> bool:
    """Whether `ip` is this server. Unknown counts as yes: refuse rather than assume."""
    own = local.get()
    if own is None:
        return True
    addr = ipaddress.ip_address(_canonical(ip))
    forms = {str(addr)}
    if isinstance(addr, ipaddress.IPv6Address):
        inner = addr.ipv4_mapped or _embedded_ipv4(addr)
        if inner is not None:
            forms.add(str(inner))
    return bool(forms & own)


async def reach(
    ip: str, port: int, *, limiter: ReachLimiter, local: LocalAddresses
) -> dict[str, object]:
    """Check one port on the requester's own address. Raises `ReachError` for refusals."""
    if _own(ip, local):
        raise ReachError(400, "this server's own address is never checked")
    wait = limiter.take_client(ip)
    if wait:
        raise ReachError(429, f"too many checks; try again in {wait} s", wait)
    if not limiter.acquire():
        raise ReachError(429, "busy; try again in a few seconds", BUSY_RETRY_S)
    try:
        is_open, foreign_api, detail = await probe(ip, port)
    finally:
        limiter.release()
    return {"ip": ip, "port": port, "open": is_open, "foreign_api": foreign_api, "detail": detail}


async def probe(ip: str, port: int, timeout: float = TIMEOUT_S) -> tuple[bool, bool, str]:
    """(open, foreign_api, detail). One connection; nothing is retried."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except TimeoutError:
        return False, False, f"no answer within {timeout:g} s (closed or filtered)"
    except ConnectionRefusedError:
        return False, False, "connection refused (nothing listens on this port)"
    except OSError as exc:
        return False, False, f"not reachable ({exc.strerror or type(exc).__name__})"
    try:
        version = await asyncio.wait_for(_check_version(reader, writer, ip, port), timeout)
    except (TimeoutError, OSError, ValueError, UnicodeDecodeError):
        version = None
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1.0)
        except (TimeoutError, OSError):
            pass
    if version is None:
        return True, False, "port open; no Epic wallet foreign API answered on it"
    return True, True, f"port open; Epic wallet foreign API answered (version {version})"


async def _check_version(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ip: str, port: int
) -> str | None:
    """The foreign API version from a `check_version` reply, or None if it is not one."""
    host = f"[{ip}]" if ":" in ip else ip
    writer.write(
        (
            f"POST {FOREIGN_API_PATH} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "User-Agent: epic-status-reach\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(_CHECK_VERSION)}\r\nConnection: close\r\n\r\n"
        ).encode()
        + _CHECK_VERSION
    )
    await writer.drain()
    reply = b""
    while len(reply) < MAX_REPLY_BYTES:
        chunk = await reader.read(MAX_REPLY_BYTES - len(reply))
        if not chunk:
            break
        reply += chunk
    head, sep, body = reply.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split()
    if not sep or len(status) < 2 or not status[0].startswith(b"HTTP/1.") or status[1] != b"200":
        return None
    if b"transfer-encoding: chunked" in head.lower():
        body = _unchunk(body)
    data = json.loads(body)
    ok = data.get("result", {}).get("Ok") if isinstance(data, dict) else None
    version = ok.get("foreign_api_version") if isinstance(ok, dict) else None
    return str(version) if isinstance(version, int) and not isinstance(version, bool) else None


def _unchunk(body: bytes) -> bytes:
    out = b""
    while body:
        size_line, _, rest = body.partition(b"\r\n")
        size = int(size_line.split(b";", 1)[0], 16)
        if size == 0:
            break
        out += rest[:size]
        body = rest[size + 2 :]
    return out
