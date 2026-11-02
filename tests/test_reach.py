"""`GET /api/v1/ip` and `GET /api/v1/reach`: the wallet connectivity self-test.

The probe tests listen on loopback only; the route tests replace the probe, so nothing
here leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from epic_status import reach as rc
from epic_status.api import create_app
from epic_status.quickcheck import LocalAddresses

from test_quickcheck import OURS, PUBLIC, make_settings

PUBLIC6 = "2001:4860:4860::8888"


def app_for(tmp_path: Path) -> FastAPI:
    app = create_app(make_settings(tmp_path))
    app.state.service.quickcheck.local = LocalAddresses(reader=lambda: frozenset({"127.0.0.1", OURS}))
    return app


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    async def probe(ip: str, port: int, timeout: float = rc.TIMEOUT_S) -> tuple[bool, bool, str]:
        calls.append((ip, port))
        return True, False, "port open; no Epic wallet foreign API answered on it"

    monkeypatch.setattr(rc, "probe", probe)
    return calls


# ------------------------------------------------------------------------------- /ip


async def test_ip_is_the_proxys_peer(tmp_path: Path) -> None:
    async with client_for(app_for(tmp_path)) as client:
        response = await client.get("/api/v1/ip", headers={"x-real-ip": PUBLIC})
    assert response.status_code == 200
    assert response.json() == {"ip": PUBLIC}
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers


async def test_ip_ignores_forwarded_for(tmp_path: Path) -> None:
    """A client-written x-forwarded-for never becomes the answer."""
    async with client_for(app_for(tmp_path)) as client:
        spoofed = await client.get("/api/v1/ip", headers={"x-real-ip": PUBLIC, "x-forwarded-for": "1.1.1.1"})
        bare = await client.get("/api/v1/ip", headers={"x-forwarded-for": "1.1.1.1"})
    assert spoofed.json() == {"ip": PUBLIC}
    # No x-real-ip: the socket peer, which behind nginx is loopback, which is refused.
    assert bare.status_code == 400 and "ip" not in bare.json()


async def test_ip_unwraps_mapped_ipv6_and_keeps_ipv6(tmp_path: Path) -> None:
    async with client_for(app_for(tmp_path)) as client:
        mapped = await client.get("/api/v1/ip", headers={"x-real-ip": f"::ffff:{PUBLIC}"})
        six = await client.get("/api/v1/ip", headers={"x-real-ip": PUBLIC6})
    assert mapped.json() == {"ip": PUBLIC}
    assert six.json() == {"ip": PUBLIC6}


@pytest.mark.parametrize("path", ["/api/v1/ip", "/api/v1/reach?port=3415"])
async def test_onion_is_refused(tmp_path: Path, path: str, fake_probe: list[Any]) -> None:
    async with client_for(app_for(tmp_path)) as client:
        response = await client.get(path, headers={"x-epic-onion": "1", "x-real-ip": "127.0.0.1"})
    assert response.status_code == 400
    assert response.json() == {"error": "not available over Tor"}
    assert fake_probe == []


@pytest.mark.parametrize("peer", ["127.0.0.1", "10.0.0.5", "100.64.1.1", "::1", "fd00::1", "garbage"])
async def test_non_public_peer_is_refused(tmp_path: Path, peer: str, fake_probe: list[Any]) -> None:
    async with client_for(app_for(tmp_path)) as client:
        ip = await client.get("/api/v1/ip", headers={"x-real-ip": peer})
        reach = await client.get("/api/v1/reach?port=3415", headers={"x-real-ip": peer})
    assert ip.status_code == 400 and reach.status_code == 400
    assert fake_probe == []


# ---------------------------------------------------------------------------- /reach


async def test_reach_probes_the_requester_only(tmp_path: Path, fake_probe: list[Any]) -> None:
    async with client_for(app_for(tmp_path)) as client:
        response = await client.get(
            "/api/v1/reach", params={"port": "3415", "ip": "1.1.1.1", "host": "1.1.1.1"},
            headers={"x-real-ip": PUBLIC},
        )
    assert response.status_code == 200
    assert response.json() == {
        "ip": PUBLIC, "port": 3415, "open": True, "foreign_api": False,
        "detail": "port open; no Epic wallet foreign API answered on it",
    }
    assert fake_probe == [(PUBLIC, 3415)]


@pytest.mark.parametrize("port", ["", "abc", "0", "22", "1023", "65536", "+3415", " 3415", "3_415", "34150000", "٣٤١٥"])
async def test_reach_port_range(tmp_path: Path, port: str, fake_probe: list[Any]) -> None:
    async with client_for(app_for(tmp_path)) as client:
        response = await client.get("/api/v1/reach", params={"port": port}, headers={"x-real-ip": PUBLIC})
        missing = await client.get("/api/v1/reach", headers={"x-real-ip": PUBLIC})
    assert response.status_code == 400 and missing.status_code == 400
    assert response.json() == {"error": "port must be an integer from 1024 to 65535"}
    assert fake_probe == []


@pytest.mark.parametrize("port", ["1024", "65535"])
async def test_reach_port_bounds_accepted(tmp_path: Path, port: str, fake_probe: list[Any]) -> None:
    async with client_for(app_for(tmp_path)) as client:
        response = await client.get("/api/v1/reach", params={"port": port}, headers={"x-real-ip": PUBLIC})
    assert response.status_code == 200


async def test_reach_refuses_this_server(tmp_path: Path, fake_probe: list[Any]) -> None:
    async with client_for(app_for(tmp_path)) as client:
        response = await client.get("/api/v1/reach?port=3415", headers={"x-real-ip": OURS})
    assert response.status_code == 400
    assert fake_probe == []


async def test_reach_rate_limited(tmp_path: Path, fake_probe: list[Any]) -> None:
    async with client_for(app_for(tmp_path)) as client:
        statuses = [
            (await client.get("/api/v1/reach?port=3415", headers={"x-real-ip": PUBLIC})).status_code
            for _ in range(rc.PER_MINUTE)
        ]
        over = await client.get("/api/v1/reach?port=3415", headers={"x-real-ip": PUBLIC})
        # Same IPv6 /64 is one visitor; another address is not affected.
        other = await client.get("/api/v1/reach?port=3415", headers={"x-real-ip": "9.9.9.9"})
    assert statuses == [200] * rc.PER_MINUTE
    assert over.status_code == 429 and int(over.headers["retry-after"]) >= 1
    assert "error" in over.json()
    assert other.status_code == 200
    assert len(fake_probe) == rc.PER_MINUTE + 1


async def test_reach_busy_is_429(tmp_path: Path, fake_probe: list[Any]) -> None:
    app = app_for(tmp_path)
    app.state.service.reach_limiter.active = rc.MAX_CONCURRENT
    async with client_for(app) as client:
        response = await client.get("/api/v1/reach?port=3415", headers={"x-real-ip": PUBLIC})
    assert response.status_code == 429 and fake_probe == []


def test_limiter_keys_ipv6_by_64() -> None:
    limiter = rc.ReachLimiter("s", clock=lambda: 0.0)
    for i in range(rc.PER_MINUTE):
        assert limiter.take_client(f"2001:db8:1:2::{i + 1}") is None
    assert limiter.take_client("2001:db8:1:2::ffff") is not None
    assert limiter.take_client("2001:db8:1:3::1") is None


# ----------------------------------------------------------------------------- probe


async def _serve(handler: Any) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_probe_closed_port() -> None:
    server, port = await _serve(lambda r, w: w.close())
    server.close()
    await server.wait_closed()
    is_open, api, detail = await rc.probe("127.0.0.1", port)
    assert (is_open, api) == (False, False) and "refused" in detail


async def test_probe_open_port_without_wallet() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        writer.write(b"SSH-2.0-nope\r\n")
        writer.close()

    server, port = await _serve(handler)
    async with server:
        is_open, api, detail = await rc.probe("127.0.0.1", port, timeout=2.0)
    assert (is_open, api) == (True, False)
    assert detail == "port open; no Epic wallet foreign API answered on it"


async def test_probe_silent_open_port_times_out_as_open() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Reads the request and never answers, until the probe hangs up.
        while await reader.read(1024):
            pass
        writer.close()

    server, port = await _serve(handler)
    async with server:
        is_open, api, _ = await rc.probe("127.0.0.1", port, timeout=0.3)
    assert (is_open, api) == (True, False)


@pytest.mark.parametrize("chunked", [False, True])
async def test_probe_epic_wallet_foreign_api(chunked: bool) -> None:
    seen: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.read(4096))
        body = json.dumps({"id": 1, "jsonrpc": "2.0", "result": {"Ok": {
            "foreign_api_version": 2, "supported_slate_versions": ["V3", "V2"]}}}).encode()
        if chunked:
            head = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ntransfer-encoding: chunked\r\n\r\n"
            payload = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        else:
            head = f"HTTP/1.1 200 OK\r\ncontent-length: {len(body)}\r\n\r\n".encode()
            payload = body
        writer.write(head + payload)
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        is_open, api, detail = await rc.probe("127.0.0.1", port, timeout=2.0)
    assert (is_open, api) == (True, True)
    assert detail == "port open; Epic wallet foreign API answered (version 2)"
    assert seen[0].startswith(b"POST /v2/foreign HTTP/1.1\r\n")
    assert b'"method":"check_version"' in seen[0]



# ------------------------------------------------------------- browsers steered by other sites


@pytest.mark.parametrize("path", ["/api/v1/ip", "/api/v1/reach?port=3415"])
@pytest.mark.parametrize(
    "extra",
    [
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
        {"origin": "https://evil.example"},
    ],
)
async def test_selftest_refuses_cross_site(
    tmp_path: Path, path: str, extra: dict[str, str], fake_probe: list[Any]
) -> None:
    async with client_for(app_for(tmp_path)) as client:
        response = await client.get(path, headers={"x-real-ip": PUBLIC, **extra})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert fake_probe == []


@pytest.mark.parametrize("path", ["/api/v1/ip", "/api/v1/reach?port=3415"])
async def test_selftest_allows_plain_and_same_origin_without_cors(
    tmp_path: Path, path: str, fake_probe: list[Any]
) -> None:
    async with client_for(app_for(tmp_path)) as client:
        plain = await client.get(path, headers={"x-real-ip": PUBLIC})
        same = await client.get(path, headers={"x-real-ip": PUBLIC, "sec-fetch-site": "same-origin"})
    assert plain.status_code == 200 and same.status_code == 200
    assert "access-control-allow-origin" not in plain.headers


@pytest.mark.parametrize(
    "body", [{"id": 1, "result": None}, {"id": 1, "result": [1]}, [1, 2]]
)
async def test_probe_odd_json_is_open_without_wallet(body: Any) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(4096)
        raw = json.dumps(body).encode()
        writer.write(f"HTTP/1.1 200 OK\r\ncontent-length: {len(raw)}\r\n\r\n".encode() + raw)
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        is_open, api, _ = await rc.probe("127.0.0.1", port, timeout=2.0)
    assert (is_open, api) == (True, False)
