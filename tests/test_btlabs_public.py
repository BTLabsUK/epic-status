"""The donate page's address from the signed public file. Hermetic: no network."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from epic_status import btlabs_public as bp
from epic_status.btlabs_public import BtlabsPublic, Rejected, extract_address, load_keyring, verify
from epic_status.config import Settings
from epic_status.render import DONATE_ADDRESSES, render_donate

FIXTURE = Path(__file__).parent / "fixtures" / "btlabs-serial6.json"
ADDRESS = "esWxboBPMgL4RqufRYcGUJ7t6YVWGvyuid6xRM62UkRzSb3jUcNK@epicbox.btlabs.uk"
NOW = datetime(2026, 11, 1, tzinfo=UTC)
URL = "https://example.test/btlabs.json"


@pytest.fixture
def envelope() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture
def ring() -> dict:
    return load_keyring()


def _payload(envelope: bytes) -> dict:
    return json.loads(base64.b64decode(json.loads(envelope)["payload"]))


def _resign_less(envelope: bytes, mutate) -> bytes:
    """Change the payload without re-signing: the signature must no longer verify."""
    env = json.loads(envelope)
    payload = _payload(envelope)
    mutate(payload)
    env["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    return json.dumps(env).encode()


def test_real_serial_6_envelope_verifies(envelope: bytes, ring: dict) -> None:
    payload, state = verify(envelope, ring, None, NOW)
    assert payload["serial"] == 6 == state["serial"]
    assert extract_address(payload) == ADDRESS


def test_tampered_payload_is_rejected(envelope: bytes, ring: dict) -> None:
    def swap(p: dict) -> None:
        p["donations"][0]["address"] = "es" + "1" * 50 + "@evil.example"

    with pytest.raises(Rejected, match="bad_signature"):
        verify(_resign_less(envelope, swap), ring, None, NOW)


def test_lower_serial_is_rejected(envelope: bytes, ring: dict) -> None:
    _, state = verify(envelope, ring, None, NOW)
    with pytest.raises(Rejected, match="rollback"):
        verify(envelope, ring, {**state, "serial": 7}, NOW)


def test_same_serial_other_digest_is_rejected(envelope: bytes, ring: dict) -> None:
    _, state = verify(envelope, ring, None, NOW)
    with pytest.raises(Rejected, match="rollback"):
        verify(envelope, ring, {**state, "digest": "0" * 64}, NOW)


def test_expired_unknown_key_and_revoked_key_are_rejected(envelope: bytes, ring: dict) -> None:
    with pytest.raises(Rejected, match="expired"):
        verify(envelope, ring, None, datetime(2027, 6, 1, tzinfo=UTC))
    with pytest.raises(Rejected, match="unknown_key"):
        verify(envelope, {}, None, NOW)
    signer = verify(envelope, ring, None, NOW)[1]["keyid"]
    with pytest.raises(Rejected, match="unknown_key"):
        verify(envelope, ring, {"serial": 1, "keyid": signer, "digest": "0" * 64, "revoked": [signer]}, NOW)


def test_wrong_payload_type_and_oversize(envelope: bytes, ring: dict) -> None:
    env = json.loads(envelope)
    env["payloadType"] = "application/vnd.btlabs.wallet-for-epic.update+json"
    with pytest.raises(Rejected, match="wrong_type"):
        verify(json.dumps(env).encode(), ring, None, NOW)
    with pytest.raises(Rejected, match="too_large"):
        verify(b" " * 70000, ring, None, NOW)


def _provider(tmp_path: Path, handler, **kw) -> BtlabsPublic:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BtlabsPublic(
        url=URL, fallback="esFALLBACK@epicbox.example", cache_path=tmp_path / "bp.json",
        client=client, now=lambda: NOW, **kw,
    )


async def test_fetch_serves_and_caches_on_disk(tmp_path: Path, envelope: bytes) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=envelope)

    now = [0.0]
    provider = _provider(tmp_path, handler, clock=lambda: now[0])
    assert await provider.address() == ADDRESS
    assert await provider.address() == ADDRESS
    assert len(calls) == 1  # served from memory within the ttl
    now[0] = 121
    assert await provider.address() == ADDRESS
    assert len(calls) == 2
    assert json.loads((tmp_path / "bp.json").read_text())["state"]["serial"] == 6


async def test_failure_falls_back_to_last_good_copy_from_disk(tmp_path: Path, envelope: bytes) -> None:
    ok = _provider(tmp_path, lambda r: httpx.Response(200, content=envelope))
    assert await ok.address() == ADDRESS

    # A new process, the file unreachable: the stored envelope is re-verified and used.
    down = _provider(tmp_path, lambda r: httpx.Response(503))
    assert await down.address() == ADDRESS

    # A garbage reply and a tampered reply do not displace it either.
    junk = _provider(tmp_path, lambda r: httpx.Response(200, content=b"<html>"))
    assert await junk.address() == ADDRESS
    bad = _resign_less(envelope, lambda p: p["donations"][0].update(address="es" + "1" * 50 + "@x.example"))
    forged = _provider(tmp_path, lambda r: httpx.Response(200, content=bad))
    assert await forged.address() == ADDRESS


async def test_tampered_disk_copy_is_ignored(tmp_path: Path, envelope: bytes) -> None:
    bad = _resign_less(envelope, lambda p: p["donations"][0].update(address="es" + "1" * 50 + "@x.example"))
    state = verify(envelope, load_keyring(), None, NOW)[1]
    (tmp_path / "bp.json").write_text(json.dumps({"envelope": bad.decode(), "state": state}))
    provider = _provider(tmp_path, lambda r: httpx.Response(503))
    assert await provider.address() == "esFALLBACK@epicbox.example"


async def test_no_good_copy_uses_the_bundled_address(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    provider = _provider(tmp_path, boom)
    assert await provider.address() == "esFALLBACK@epicbox.example"


def test_qr_is_per_address_and_looks_like_the_static_ones(tmp_path: Path) -> None:
    provider = BtlabsPublic(url="", fallback=ADDRESS, cache_path=tmp_path / "x.json")
    svg = provider.qr(ADDRESS)
    assert 'class="segno"' in svg and 'class="qrline"' in svg and "#0b0d10" in svg
    assert provider.qr(ADDRESS) is svg
    assert provider.qr("esOTHER" + ADDRESS[7:]) != svg


def test_donate_page_uses_the_given_address_and_inlines_its_qr(tmp_path: Path) -> None:
    settings = Settings(db=tmp_path / "s.db")
    html = render_donate(settings=settings, epicbox_address=ADDRESS, epicbox_qr_svg="<svg/>")
    assert ADDRESS in html and "data:image/svg+xml;base64," in html
    assert "/static/qr/epicbox.svg" not in html and "/static/qr/https.svg" in html
    assert DONATE_ADDRESSES[0]["value"] == ADDRESS  # the bundled fallback is current
    assert "/static/qr/epicbox.svg" in render_donate(settings=settings)
