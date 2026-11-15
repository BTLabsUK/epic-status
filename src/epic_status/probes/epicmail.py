"""Epicmail relays: detection and the richer health they publish.

Epicmail (https://github.com/BTLabsUK/epicmail) speaks the same websocket protocol as the
classic relay, so the handshake in `probes.epicbox` stays the health check of record for
every relay. On top of it, Epicmail serves small JSON documents built for monitors:

* `/.well-known/epicbox.json`: software, version, protocol versions (3.1.0 is transaction
  cancel), federation, and features (`mux/1`, the HTTPS pull API `https`).
* `/v1/info`: the HTTPS pull API's own description, present when that API is on.
* `/status.json`: `draining` and `store_ok` are read from it. It also carries traffic
  counts (sockets, mailboxes, waiting messages, the forward queue), which are not read:
  they say how a relay is used, not whether it works.
* `/ready`: readiness with a store write check, which the relay itself rate-limits to one
  write per five seconds. Read only when `/status.json` is missing.

`/health` is not read: it answers "ok" whenever the HTTP server runs, and the websocket
challenge already proves that and more.

**Why this is not the GET the relay probe refuses.** The classic relay renders its own
status page for *any* path, and that page opens TLS connections to other relays to draw
their dots. Polling it would make this service an amplifier. So:

* `/status.json` is read only from a relay that has already identified
  itself as Epicmail, whose documents are static JSON;
* the one request that can reach a classic relay is detection, a single GET of
  `/.well-known/epicbox.json`, made when a relay is first seen, when its websocket
  `GetVersion` answer changes, and otherwise once a week (see `Cadence`).

Every request is addressed to the validated IP from `net.resolve_endpoint`, follows no
redirects, has a short timeout and reads at most `MAX_BODY` bytes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from ..models import EPICMAIL, RelayHealth, RelayInfo
from ..net import Resolved, classify_http_error, pinned_url

WELL_KNOWN = "/.well-known/epicbox.json"
STATUS = "/status.json"
READY = "/ready"
V1_INFO = "/v1/info"

MAX_BODY = 64 * 1024
"""Epicmail's largest document is about 500 bytes. The cap is for whatever else answers."""

# `software` is shown on the page, so it is held to a short token.
_SOFTWARE_MAX = 32
_VERSION_MAX = 32


@dataclasses.dataclass(frozen=True)
class Fetched:
    status: int | None
    payload: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None


def http_side(resolved: Resolved) -> Resolved:
    """The same host, port and validated addresses, over HTTP(S) instead of WS(S).

    Epicmail serves its documents on the listener that takes the websocket, so the
    relay's own endpoint is the base for every path here."""
    scheme = {"wss": "https", "ws": "http"}.get(resolved.scheme, resolved.scheme)
    return dataclasses.replace(resolved, scheme=scheme)


async def fetch_json(
    client: httpx.AsyncClient, resolved: Resolved, path: str, *, timeout: float
) -> Fetched:
    """One GET, JSON or nothing. Never raises."""
    target = http_side(resolved)
    try:
        async with asyncio.timeout(timeout + 1):
            async with client.stream(
                "GET",
                pinned_url(target, path),
                headers={"host": target.hostname, "accept": "application/json"},
                extensions={"sni_hostname": target.hostname},
                timeout=timeout,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY:
                        return Fetched(response.status_code, error="response too large")
                status = response.status_code
    except Exception as exc:  # noqa: BLE001 - every failure becomes text
        return Fetched(None, error=classify_http_error(exc)[1])
    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # The classic relay answers every path with its HTML status page.
        payload = None
    if status != 200:
        # Kept with the error: `/ready` answers 503 with the reason in the body.
        return Fetched(status, payload=payload, error=f"HTTP {status}")
    if payload is None:
        return Fetched(status, error="not JSON")
    return Fetched(status, payload=payload)


# ------------------------------------------------------------------------- parsing


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text: str = value.strip()
    if not text or len(text) > limit or not text.isprintable():
        return None
    return text


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def parse_well_known(payload: Any, *, now: datetime | None = None) -> RelayInfo | None:
    """`/.well-known/epicbox.json` → RelayInfo, or None when it is not that document.

    `software` is required: it is the field that says which relay this is. Anything that
    answers JSON without it is not describing itself, and gets no badge."""
    if not isinstance(payload, dict):
        return None
    software = _text(payload.get("software"), _SOFTWARE_MAX)
    if software is None:
        return None
    versions_raw = payload.get("protocol_versions")
    versions = (
        [v for v in (_text(x, 16) for x in versions_raw) if v is not None][:8]
        if isinstance(versions_raw, list)
        else []
    )
    features = payload.get("features")
    features = features if isinstance(features, dict) else {}
    https = features.get("https")
    https_protocol = (
        _text(https.get("protocol"), _VERSION_MAX) if isinstance(https, dict) else None
    )
    return RelayInfo(
        software=software.lower(),
        version=_text(payload.get("version"), _VERSION_MAX),
        protocol_versions=versions,
        federation=_flag(payload.get("federation")),
        mux="mux/1" in features,
        https_protocol=https_protocol,
        checked_at=now or datetime.now(UTC),
    )


def parse_v1_info(payload: Any, info: RelayInfo) -> bool:
    """Whether `/v1/info` describes the HTTPS API the info document announced."""
    if not isinstance(payload, dict):
        return False
    protocol = _text(payload.get("protocol"), _VERSION_MAX)
    return protocol is not None and protocol == info.https_protocol


def parse_status(payload: Any, *, now: datetime | None = None) -> RelayHealth:
    """`/status.json` → RelayHealth. Readiness only: `draining`, `store_ok` and the
    version. The traffic counts in the same document are not read."""
    stamp = now or datetime.now(UTC)
    if not isinstance(payload, dict):
        return RelayHealth(ok=False, error="status document is not an object", checked_at=stamp)
    return RelayHealth(
        ok=True,
        source="status.json",
        version=_text(payload.get("version"), _VERSION_MAX),
        draining=_flag(payload.get("draining")),
        store_ok=_flag(payload.get("store_ok")),
        checked_at=stamp,
    )


def parse_ready(fetched: Fetched, *, now: datetime | None = None) -> RelayHealth:
    """`/ready` → RelayHealth. 200 `{"ready":true}`, or 503 with a reason: `draining`
    or `store`. The 503 is an answer, not a failure to answer."""
    stamp = now or datetime.now(UTC)
    payload = fetched.payload if isinstance(fetched.payload, dict) else None
    if fetched.status == 200 and payload is not None and payload.get("ready") is True:
        return RelayHealth(ok=True, source="ready", draining=False, store_ok=True, checked_at=stamp)
    if fetched.status == 503:
        reason = payload.get("reason") if payload else None
        return RelayHealth(
            ok=True,
            source="ready",
            draining=reason == "draining",
            store_ok=False if reason == "store" else None,
            checked_at=stamp,
        )
    return RelayHealth(ok=False, source="ready", error=fetched.error or "no answer", checked_at=stamp)


# --------------------------------------------------------------------------- probes


async def detect(
    client: httpx.AsyncClient, resolved: Resolved, *, timeout: float
) -> tuple[bool, RelayInfo | None]:
    """`(answered, info)`. `answered` is False on a transport failure, when the
    previous detection should stand; True with None is a definite "not self-described"
    (the classic relay's HTML page, a 404)."""
    fetched = await fetch_json(client, resolved, WELL_KNOWN, timeout=timeout)
    if fetched.status is None:
        return False, None
    if not fetched.ok:
        return True, None
    info = parse_well_known(fetched.payload)
    if info is not None and info.is_epicmail and info.https_protocol:
        v1 = await fetch_json(client, resolved, V1_INFO, timeout=timeout)
        info.https_ok = v1.ok and parse_v1_info(v1.payload, info)
    return True, info


async def read_health(
    client: httpx.AsyncClient, resolved: Resolved, *, timeout: float
) -> RelayHealth:
    """`/status.json`, else `/ready`. Only for a relay already detected as Epicmail."""
    fetched = await fetch_json(client, resolved, STATUS, timeout=timeout)
    if fetched.ok:
        return parse_status(fetched.payload)
    if fetched.status is None:
        return RelayHealth(ok=False, error=fetched.error, checked_at=datetime.now(UTC))
    return parse_ready(await fetch_json(client, resolved, READY, timeout=timeout))


__all__ = [
    "EPICMAIL",
    "Fetched",
    "detect",
    "fetch_json",
    "http_side",
    "parse_ready",
    "parse_status",
    "parse_v1_info",
    "parse_well_known",
    "read_health",
]
