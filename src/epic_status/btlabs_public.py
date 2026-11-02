"""BT Labs' own donation address, read from the signed public file `btlabs.json`.

The file is a DSSE envelope signed with Ed25519 (FORMAT.md §5 of the btlabs-public
repository). This module fetches it, verifies it the way a wallet does, and keeps the
last accepted copy on disk. A fetch or verify failure is only logged: the page serves
the last good copy, and with none it serves the bundled address. Keys are the keyring
shipped in `keys/`; nothing is ever fetched for trust.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import segno
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger(__name__)

PAYLOAD_TYPE = "application/vnd.btlabs.public+json"
MAX_BYTES = 65536
MAX_SIGNATURES = 8
MAX_VALIDITY = timedelta(days=400)
KEYRING_FILE = Path(__file__).parent / "keys" / "btlabs-keyring.json"

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_KEYID = re.compile(r"[0-9a-f]{16}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ADDRESS = re.compile(
    r"es[1-9A-HJ-NP-Za-km-z]{40,60}@[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+\Z"
)


class Rejected(Exception):
    """The reason is the message, in the vocabulary of FORMAT.md §5."""


def _b64d(s: object) -> bytes:
    """Strict, canonical standard base64 with padding."""
    if not isinstance(s, str) or len(s) % 4:
        raise ValueError("bad base64")
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("bad base64") from None
    if base64.b64encode(raw).decode("ascii") != s:
        raise ValueError("bad base64")
    return raw


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_const(name: str) -> None:
    raise ValueError("non-finite number")


def _loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_const)


def _ts(value: object) -> datetime:
    if not isinstance(value, str) or not _TS.match(value):
        raise ValueError("bad timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _pae(payload_type: str, body: bytes) -> bytes:
    t = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(body), body)


def load_keyring(path: Path = KEYRING_FILE) -> dict[str, dict[str, Any]]:
    """{keyid: {rank, raw}}, with each keyid checked against its public key."""
    ring: dict[str, dict[str, Any]] = {}
    for key in json.loads(path.read_text("utf-8"))["keys"]:
        raw = _b64d(key["public_key"])
        if len(raw) != 32 or hashlib.sha256(raw).hexdigest()[:16] != key["keyid"]:
            raise ValueError(f"keyring: bad key {key.get('label')}")
        ring[key["keyid"]] = {"rank": int(key["rank"]), "raw": raw}
    return ring


def extract_address(payload: dict[str, Any]) -> str:
    for entry in payload.get("donations", []):
        if (
            isinstance(entry, dict)
            and entry.get("id") == "btlabs"
            and entry.get("coin") == "epic"
            and entry.get("network") == "mainnet"
            and entry.get("method") == "epicbox"
        ):
            address = entry.get("address")
            if isinstance(address, str) and _ADDRESS.match(address):
                return address
            raise Rejected("bad_address")
    raise Rejected("no_address")


def verify(
    data: bytes,
    ring: dict[str, dict[str, Any]],
    state: dict[str, Any] | None,
    now: datetime,
    *,
    check_expiry: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """FORMAT.md §5. Returns (payload, new state); raises Rejected(reason).

    `state` is `{serial, keyid, digest, revoked}` of the last accepted file. An equal
    serial with the same digest is accepted (the caller sees the same state back).
    """
    if len(data) > MAX_BYTES:
        raise Rejected("too_large")
    try:
        env = _loads(data.decode("utf-8"))
        ptype, b64payload, sigs = env["payloadType"], env["payload"], env["signatures"]
        if not isinstance(ptype, str) or not isinstance(sigs, list):
            raise ValueError
        body = _b64d(b64payload)
        if not 1 <= len(sigs) <= MAX_SIGNATURES:
            raise ValueError
        parsed = []
        for s in sigs:
            if not isinstance(s["keyid"], str):
                raise ValueError
            raw_sig = _b64d(s["sig"])
            if len(raw_sig) != 64:
                raise ValueError
            parsed.append((s["keyid"], raw_sig))
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise Rejected("malformed") from None
    if ptype != PAYLOAD_TYPE:
        raise Rejected("wrong_type")

    stored_revoked = set(state["revoked"]) if state else set()
    trusted = {k: v for k, v in ring.items() if k not in stored_revoked}
    candidates = [(k, sig) for k, sig in parsed if k in trusted]
    if not candidates:
        raise Rejected("unknown_key")
    message = _pae(ptype, body)
    signer: str | None = None
    for keyid, sig in candidates:
        try:
            Ed25519PublicKey.from_public_bytes(trusted[keyid]["raw"]).verify(sig, message)
        except InvalidSignature:
            continue
        if signer is None or trusted[keyid]["rank"] > trusted[signer]["rank"]:
            signer = keyid
    if signer is None:
        raise Rejected("bad_signature")

    try:
        payload = _loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        if type(payload.get("schema")) is not int:
            raise ValueError
        serial = payload["serial"]
        if type(serial) is not int or not 1 <= serial <= 2**53 - 1:
            raise ValueError
        issued, expires = _ts(payload["issued_at"]), _ts(payload["expires_at"])
        if not issued < expires <= issued + MAX_VALIDITY:
            raise ValueError
        revoked_keys = payload.get("revoked_keys", [])
        if not isinstance(revoked_keys, list):
            raise ValueError
        if not isinstance(payload.get("donations"), list):
            raise ValueError
    except (ValueError, KeyError, TypeError, RecursionError):
        raise Rejected("malformed") from None
    if payload["schema"] != 1:
        raise Rejected("unsupported_schema")
    if check_expiry and expires <= now:
        raise Rejected("expired")

    revoked = set(stored_revoked)
    for key in revoked_keys:
        if isinstance(key, str) and key in ring and ring[key]["rank"] < ring[signer]["rank"]:
            revoked.add(key)

    digest = hashlib.sha256(body).hexdigest()
    if state and state["keyid"] in ring and state["keyid"] not in revoked:
        if serial < state["serial"]:
            raise Rejected("rollback")
        if serial == state["serial"]:
            if digest != state["digest"]:
                raise Rejected("rollback")
            return payload, {**state, "revoked": sorted(revoked)}
    return payload, {
        "serial": serial,
        "keyid": signer,
        "digest": digest,
        "revoked": sorted(revoked),
    }


def _state_ok(state: object) -> bool:
    return (
        isinstance(state, dict)
        and type(state.get("serial")) is int
        and isinstance(state.get("keyid"), str)
        and bool(_KEYID.match(state["keyid"]))
        and isinstance(state.get("digest"), str)
        and bool(_DIGEST.match(state["digest"]))
        and isinstance(state.get("revoked"), list)
        and all(isinstance(k, str) and _KEYID.match(k) for k in state["revoked"])
    )


def qr_svg(address: str) -> str:
    """The Epicbox QR in the look of the static SVGs in /static/qr."""
    buf = io.BytesIO()
    segno.make(address, error="m").save(
        buf, kind="svg", scale=1, border=2, dark="#0b0d10", light="#fff", xmldecl=True
    )
    return buf.getvalue().decode("utf-8")


class BtlabsPublic:
    """The Epicbox address in use, fresh within `ttl` seconds, never failing."""

    def __init__(
        self,
        *,
        url: str,
        fallback: str,
        cache_path: Path,
        keyring: dict[str, dict[str, Any]] | None = None,
        ttl: float = 120.0,
        retry: float = 30.0,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
        clock: Any = time.monotonic,
        now: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._url = url
        self._fallback = fallback
        self._cache_path = cache_path
        self._ring = keyring if keyring is not None else load_keyring()
        self._ttl, self._retry, self._timeout = ttl, retry, timeout
        self._client = client
        self._clock, self._now = clock, now
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] | None = None
        self._address: str | None = None
        self._next_fetch = 0.0
        self._qr: dict[str, str] = {}
        self._loaded = False

    # -- disk copy ------------------------------------------------------------

    def _load_disk(self) -> None:
        """Re-verify the stored envelope; a bad or missing copy is simply absent."""
        self._loaded = True
        try:
            saved = json.loads(self._cache_path.read_text("utf-8"))
            if not _state_ok(saved.get("state")) or not isinstance(saved.get("envelope"), str):
                raise Rejected("malformed")
            data = saved["envelope"].encode("utf-8")
            # Not re-checked for expiry: it was valid when accepted (§5).
            payload, state = verify(data, self._ring, saved["state"], self._now(), check_expiry=False)
            if state["serial"] != saved["state"]["serial"] or state["digest"] != saved["state"]["digest"]:
                raise Rejected("rollback")
            self._address = extract_address(payload)
            self._state = state
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, Rejected) as exc:
            log.warning("btlabs.json cached copy unusable: %s", exc)

    def _save_disk(self, data: bytes, state: dict[str, Any]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._cache_path.parent, prefix=".btlabs-public-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"envelope": data.decode("utf-8"), "state": state}, fh)
                os.replace(tmp, self._cache_path)
            except BaseException:
                os.unlink(tmp)
                raise
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("btlabs.json copy not stored: %s", exc)

    # -- network --------------------------------------------------------------

    async def _fetch(self) -> bytes:
        async def get(client: httpx.AsyncClient) -> bytes:
            chunks: list[bytes] = []
            size = 0
            async with client.stream("GET", self._url, follow_redirects=False) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise Rejected("too_large")
                    chunks.append(chunk)
            return b"".join(chunks)

        if self._client is not None:
            return await get(self._client)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await get(client)

    async def _refresh(self) -> None:
        try:
            data = await asyncio.wait_for(self._fetch(), self._timeout)
            payload, state = verify(data, self._ring, self._state, self._now())
            address = extract_address(payload)
        except (httpx.HTTPError, Rejected, OSError, TimeoutError) as exc:
            log.warning("btlabs.json not accepted: %s", exc or type(exc).__name__)
            self._next_fetch = self._clock() + self._retry
            return
        if self._state is None or state != self._state:
            self._save_disk(data, state)
        self._state, self._address = state, address
        self._next_fetch = self._clock() + self._ttl

    async def address(self) -> str:
        if not self._loaded:
            self._load_disk()
        if self._url and self._clock() >= self._next_fetch:
            async with self._lock:
                if self._clock() >= self._next_fetch:
                    await self._refresh()
        return self._address or self._fallback

    def qr(self, address: str) -> str:
        """Cached per address; there are only ever one or two."""
        if address not in self._qr:
            if len(self._qr) >= 8:
                self._qr.clear()
            self._qr[address] = qr_svg(address)
        return self._qr[address]
