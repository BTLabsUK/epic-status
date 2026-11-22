"""Epicmail relays: detection, parsing, the poller's schedule, the API and the board.

The bodies in `fixtures/epicmail/` were recorded once from the live relay
(epicmail.btlabs.uk, Epicmail 0.1.1) and from a classic relay; see the README there.
Nothing here touches the network: HTTP goes to respx, and the poller's resolver is
replaced with a fixed address.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from epic_status.config import Settings
from epic_status.db import Database
from epic_status.models import (
    EpicboxProbe,
    Kind,
    Reason,
    RelayHealth,
    Source,
    State,
    Target,
    TargetStatus,
)
from epic_status.net import Resolved, build_client
from epic_status.poller import Poller
from epic_status.probes import epicmail
from epic_status.probes.epicmail import (
    Fetched,
    http_side,
    parse_ready,
    parse_status,
    parse_v1_info,
    parse_well_known,
)

FIXTURES = Path(__file__).parent / "fixtures" / "epicmail"


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def doc(name: str) -> Any:
    return json.loads(raw(name))


RESOLVED = Resolved(
    hostname="epicmail.btlabs.uk",
    port=443,
    addresses=("203.0.113.7",),
    scheme="wss",
)
BASE = "https://203.0.113.7:443"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    c = build_client(timeout=2.0)
    try:
        yield c
    finally:
        await c.aclose()


# --------------------------------------------------------------------------- parsing


def test_well_known_from_the_live_relay() -> None:
    info = parse_well_known(doc("well-known.json"))
    assert info is not None
    assert info.software == "epicmail"
    assert info.is_epicmail
    assert info.version == "0.1.1"
    assert info.protocol_versions == ["2.0.0", "3.0.0", "3.1.0"]
    assert info.cancel
    assert info.mux
    assert info.https_protocol == "epicbox-https/0.1"
    assert info.federation is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        "epicmail",
        {"software": 7},
        {"software": ""},
        {"software": "x" * 100},
        {"software": "epic\nmail"},
        {"version": "0.1.1", "protocol_versions": ["3.1.0"]},
    ],
)
def test_a_document_that_does_not_name_its_software_is_not_detected(payload: Any) -> None:
    assert parse_well_known(payload) is None


def test_other_self_described_software_is_named_but_not_epicmail() -> None:
    info = parse_well_known({"software": "OtherRelay", "protocol_versions": ["3.0.0"]})
    assert info is not None
    assert info.software == "otherrelay"
    assert not info.is_epicmail
    assert not info.cancel


def test_a_relay_without_3_1_0_has_no_cancel() -> None:
    payload = doc("well-known.json") | {"protocol_versions": ["2.0.0", "3.0.0"], "features": {}}
    info = parse_well_known(payload)
    assert info is not None
    assert not info.cancel
    assert not info.mux
    assert info.https_protocol is None


def test_v1_info_must_name_the_announced_protocol() -> None:
    info = parse_well_known(doc("well-known.json"))
    assert info is not None
    assert parse_v1_info(doc("v1-info.json"), info)
    assert not parse_v1_info({"protocol": "something-else"}, info)
    assert not parse_v1_info("nope", info)


def test_status_from_the_live_relay() -> None:
    h = parse_status(doc("status.json"))
    assert h.ok
    assert h.source == "status.json"
    assert h.version == "0.1.1"
    assert h.draining is False
    assert h.store_ok is True
    assert h.ready is True
    assert h.fault is None


def test_status_copies_readiness_only() -> None:
    """No traffic counts (sockets, mailboxes, waiting messages, forward queue, uptime),
    no name or identity, and nothing a future version might add."""
    payload = doc("status.json") | {"addresses": ["esWD...@relay"], "client_ips": ["198.51.100.1"]}
    dumped = parse_status(payload).model_dump()
    assert set(dumped) == {"ok", "source", "version", "draining", "store_ok", "error", "checked_at"}
    text = json.dumps(dumped, default=str)
    for leaked in ("esWD", "198.51.100.1", "epicmail.btlabs.uk", "159", "2731", "82173952"):
        assert leaked not in text


def test_status_that_is_not_an_object_is_not_ok() -> None:
    assert not parse_status(["nope"]).ok


@pytest.mark.parametrize(
    ("fetched", "ready", "fault"),
    [
        (Fetched(200, payload=doc("ready.json")), True, None),
        (
            Fetched(503, payload={"ready": False, "reason": "draining"}, error="HTTP 503"),
            False,
            "relay reports it is restarting",
        ),
        (
            Fetched(503, payload={"ready": False, "reason": "store"}, error="HTTP 503"),
            False,
            "relay reports its message store is failing",
        ),
    ],
)
def test_ready_answers(fetched: Fetched, ready: bool, fault: str | None) -> None:
    h = parse_ready(fetched)
    assert h.ok
    assert h.source == "ready"
    assert h.ready is ready
    assert h.fault == fault


def test_ready_that_does_not_answer_is_not_ok() -> None:
    h = parse_ready(Fetched(404, error="HTTP 404"))
    assert not h.ok
    assert h.fault is None


def test_faults_from_status() -> None:
    assert parse_status(doc("status.json") | {"draining": True}).fault == (
        "relay reports it is restarting"
    )
    assert parse_status(doc("status.json") | {"store_ok": False}).fault == (
        "relay reports its message store is failing"
    )


def test_http_side_keeps_the_validated_address() -> None:
    https = http_side(RESOLVED)
    assert (https.scheme, https.hostname, https.port, https.addresses) == (
        "https",
        "epicmail.btlabs.uk",
        443,
        ("203.0.113.7",),
    )
    plain = http_side(Resolved(hostname="r.test", port=80, addresses=("10.0.0.1",), scheme="ws"))
    assert plain.scheme == "http"


# ----------------------------------------------------------------------- over HTTP


@respx.mock
async def test_detect_epicmail_reads_info_and_v1(client: httpx.AsyncClient) -> None:
    wk = respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        return_value=httpx.Response(200, content=raw("well-known.json"))
    )
    v1 = respx.get(f"{BASE}/v1/info").mock(
        return_value=httpx.Response(200, content=raw("v1-info.json"))
    )
    answered, info = await epicmail.detect(client, RESOLVED, timeout=2.0)
    assert answered
    assert info is not None and info.is_epicmail
    assert info.https_ok is True
    assert wk.call_count == 1 and v1.call_count == 1
    # Addressed to the validated IP, with the real name for virtual hosting.
    request = wk.calls.last.request
    assert request.headers["host"] == "epicmail.btlabs.uk"
    assert request.url.host == "203.0.113.7"


@respx.mock
async def test_detect_classic_relay_is_a_definite_no(client: httpx.AsyncClient) -> None:
    """The classic relay answers every path with its HTML status page."""
    respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        return_value=httpx.Response(200, content=raw("classic-any-path.html"))
    )
    v1 = respx.get(f"{BASE}/v1/info")
    answered, info = await epicmail.detect(client, RESOLVED, timeout=2.0)
    assert (answered, info) == (True, None)
    assert v1.call_count == 0


@respx.mock
async def test_detect_404_is_a_definite_no(client: httpx.AsyncClient) -> None:
    respx.get(f"{BASE}/.well-known/epicbox.json").mock(return_value=httpx.Response(404))
    assert await epicmail.detect(client, RESOLVED, timeout=2.0) == (True, None)


@respx.mock
async def test_detect_transport_failure_is_no_answer(client: httpx.AsyncClient) -> None:
    respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        side_effect=httpx.ConnectTimeout("slow")
    )
    assert await epicmail.detect(client, RESOLVED, timeout=2.0) == (False, None)


@respx.mock
async def test_detect_https_api_not_answering(client: httpx.AsyncClient) -> None:
    respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        return_value=httpx.Response(200, content=raw("well-known.json"))
    )
    respx.get(f"{BASE}/v1/info").mock(return_value=httpx.Response(404))
    _, info = await epicmail.detect(client, RESOLVED, timeout=2.0)
    assert info is not None and info.https_ok is False


@respx.mock
async def test_oversized_body_is_refused(client: httpx.AsyncClient) -> None:
    respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        return_value=httpx.Response(200, content=b"{" + b" " * (epicmail.MAX_BODY + 10) + b"}")
    )
    assert await epicmail.detect(client, RESOLVED, timeout=2.0) == (True, None)


@respx.mock
async def test_redirect_is_not_followed(client: httpx.AsyncClient) -> None:
    respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    )
    assert await epicmail.detect(client, RESOLVED, timeout=2.0) == (True, None)
    assert len(respx.calls) == 1


@respx.mock
async def test_health_reads_status_json(client: httpx.AsyncClient) -> None:
    status = respx.get(f"{BASE}/status.json").mock(
        return_value=httpx.Response(200, content=raw("status.json"))
    )
    ready = respx.get(f"{BASE}/ready")
    h = await epicmail.read_health(client, RESOLVED, timeout=2.0)
    assert h.ok and h.ready is True
    assert status.call_count == 1
    assert ready.call_count == 0


@respx.mock
async def test_health_falls_back_to_ready(client: httpx.AsyncClient) -> None:
    respx.get(f"{BASE}/status.json").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/ready").mock(
        return_value=httpx.Response(503, json={"ready": False, "reason": "store"})
    )
    h = await epicmail.read_health(client, RESOLVED, timeout=2.0)
    assert h.ok and h.source == "ready"
    assert h.fault == "relay reports its message store is failing"


@respx.mock
async def test_health_transport_failure_does_not_try_ready(client: httpx.AsyncClient) -> None:
    respx.get(f"{BASE}/status.json").mock(side_effect=httpx.ConnectError("refused"))
    ready = respx.get(f"{BASE}/ready")
    h = await epicmail.read_health(client, RESOLVED, timeout=2.0)
    assert not h.ok
    assert ready.call_count == 0


# -------------------------------------------------------------------------- poller


def _relay(db: Database, slug: str = "epicbox.btlabs.uk") -> Target:
    t = Target(
        kind=Kind.EPICBOX,
        slug=slug,
        endpoint=f"wss://{slug}:443/",
        display_host=slug,
        source=Source.SEED,
    )
    t.id = db.insert_target(t)
    return t


@pytest.fixture
async def poller(db: Database, settings: Settings) -> AsyncIterator[Poller]:
    p = Poller(db, settings)
    p._client = build_client(timeout=2.0)

    async def fixed(_: str) -> Resolved:
        return RESOLVED

    p._resolve = fixed  # type: ignore[method-assign]
    try:
        yield p
    finally:
        await p._client.aclose()


def _ok(version: str = "3.1.0") -> EpicboxProbe:
    return EpicboxProbe(ok=True, latency_ms=50, challenge_seen=True, protocol_version=version)


def _mock_epicmail() -> tuple[respx.Route, respx.Route]:
    wk = respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        return_value=httpx.Response(200, content=raw("well-known.json"))
    )
    respx.get(f"{BASE}/v1/info").mock(return_value=httpx.Response(200, content=raw("v1-info.json")))
    st = respx.get(f"{BASE}/status.json").mock(
        return_value=httpx.Response(200, content=raw("status.json"))
    )
    return wk, st


@respx.mock
async def test_poller_detects_once_then_reads_status_on_its_cadence(
    db: Database, poller: Poller
) -> None:
    relay = _relay(db)
    wk, st = _mock_epicmail()
    probes = {relay.slug: _ok()}
    cadence = poller.settings.cadence

    await poller._refresh_relay_software([relay], probes, 1000.0)
    assert wk.call_count == 1 and st.call_count == 1
    assert relay.id is not None
    status = poller._relay_status(relay, probes[relay.slug])
    assert status.is_epicmail
    assert status.relay_health is not None and status.relay_health.ready is True

    # A sweep later: nothing is due.
    await poller._refresh_relay_software([relay], probes, 1030.0)
    assert wk.call_count == 1 and st.call_count == 1

    # Counters on their own cadence, the info document on its slower one.
    await poller._refresh_relay_software([relay], probes, 1000.0 + cadence.epicmail_status_s)
    assert wk.call_count == 1 and st.call_count == 2
    await poller._refresh_relay_software([relay], probes, 1000.0 + cadence.epicmail_info_s)
    assert wk.call_count == 2

    # Kept in the database, so a restart shows the badge before the first round.
    (info, checked_at) = db.relay_infos()[relay.id]
    assert info is not None and info.is_epicmail and checked_at is not None


@respx.mock
async def test_poller_classic_relay_is_asked_rarely_and_never_for_counters(
    db: Database, poller: Poller
) -> None:
    relay = _relay(db, "epicbox.epiccash.com")
    wk = respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        return_value=httpx.Response(200, content=raw("classic-any-path.html"))
    )
    st = respx.get(f"{BASE}/status.json")
    probes = {relay.slug: _ok("3.0.0")}

    for now in (1000.0, 1030.0, 1000.0 + 86400.0, 1000.0 + 6 * 86400.0):
        await poller._refresh_relay_software([relay], probes, now)
    assert wk.call_count == 1
    assert st.call_count == 0
    assert not poller._relay_status(relay, probes[relay.slug]).is_epicmail

    await poller._refresh_relay_software(
        [relay], probes, 1000.0 + poller.settings.cadence.relay_detect_s
    )
    assert wk.call_count == 2


@respx.mock
async def test_poller_redetects_when_getversion_changes(db: Database, poller: Poller) -> None:
    """A relay switched from the classic software to Epicmail answers GetVersion with
    3.1.0 instead of 3.0.0, which is the cue to look again rather than wait a week."""
    relay = _relay(db)
    wk = respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        side_effect=[
            httpx.Response(200, content=raw("classic-any-path.html")),
            httpx.Response(200, content=raw("well-known.json")),
        ]
    )
    respx.get(f"{BASE}/v1/info").mock(return_value=httpx.Response(200, content=raw("v1-info.json")))
    respx.get(f"{BASE}/status.json").mock(return_value=httpx.Response(200, content=raw("status.json")))

    await poller._refresh_relay_software([relay], {relay.slug: _ok("3.0.0")}, 1000.0)
    await poller._refresh_relay_software([relay], {relay.slug: _ok("3.0.0")}, 1030.0)
    assert wk.call_count == 1
    await poller._refresh_relay_software([relay], {relay.slug: _ok("3.1.0")}, 1060.0)
    assert wk.call_count == 2
    assert poller._relay_status(relay, _ok()).is_epicmail


@respx.mock
async def test_poller_keeps_the_badge_through_a_timeout(db: Database, poller: Poller) -> None:
    relay = _relay(db)
    respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        side_effect=[
            httpx.Response(200, content=raw("well-known.json")),
            httpx.ConnectTimeout("slow"),
        ]
    )
    respx.get(f"{BASE}/v1/info").mock(return_value=httpx.Response(200, content=raw("v1-info.json")))
    respx.get(f"{BASE}/status.json").mock(return_value=httpx.Response(200, content=raw("status.json")))
    await poller._refresh_relay_software([relay], {relay.slug: _ok()}, 1000.0)
    await poller._refresh_relay_software(
        [relay], {relay.slug: _ok()}, 1000.0 + poller.settings.cadence.epicmail_info_s
    )
    assert poller._relay_status(relay, _ok()).is_epicmail


@respx.mock
async def test_poller_does_not_ask_a_relay_that_is_down(db: Database, poller: Poller) -> None:
    relay = _relay(db)
    wk = respx.get(f"{BASE}/.well-known/epicbox.json")
    down = EpicboxProbe(ok=False, reason=Reason.TIMEOUT, error="timed out")
    await poller._refresh_relay_software([relay], {relay.slug: down}, 1000.0)
    assert wk.call_count == 0


@respx.mock
async def test_restart_loads_detection_from_the_database(
    db: Database, settings: Settings, poller: Poller
) -> None:
    relay = _relay(db)
    _mock_epicmail()
    await poller._refresh_relay_software([relay], {relay.slug: _ok()}, 1000.0)

    fresh = Poller(db, settings)
    fresh._client = poller._client
    fresh._resolve = poller._resolve  # type: ignore[method-assign]
    wk = respx.get(f"{BASE}/.well-known/epicbox.json")
    before = wk.call_count
    await fresh._refresh_relay_software([relay], {relay.slug: _ok()}, 5.0)
    # Known as Epicmail at once, and not asked again until its six hours are up.
    assert wk.call_count == before
    assert fresh._relay_status(relay, _ok()).is_epicmail


@respx.mock
async def test_restart_knows_the_software_before_the_first_refresh(
    db: Database, settings: Settings, poller: Poller
) -> None:
    """The sweep builds its statuses before the background refresh has run, so the
    stored answer is loaded on its own; otherwise an Epicmail relay is listed with the
    classic ones for one sweep after every restart."""
    relay = _relay(db)
    _mock_epicmail()
    await poller._refresh_relay_software([relay], {relay.slug: _ok()}, 1000.0)

    fresh = Poller(db, settings)
    assert not fresh._relay_status(relay, _ok()).is_epicmail
    fresh._load_relay_info(5.0)
    assert fresh._relay_status(relay, _ok()).is_epicmail
    # Loading again changes nothing, and the next ask keeps its six-hour distance.
    due = fresh._next_detect[relay.id]
    fresh._load_relay_info(500.0)
    assert fresh._next_detect[relay.id] == due


@respx.mock
async def test_a_relay_reporting_its_store_failing_is_degraded(
    db: Database, poller: Poller
) -> None:
    relay = _relay(db)
    respx.get(f"{BASE}/.well-known/epicbox.json").mock(
        return_value=httpx.Response(200, content=raw("well-known.json"))
    )
    respx.get(f"{BASE}/v1/info").mock(return_value=httpx.Response(200, content=raw("v1-info.json")))
    respx.get(f"{BASE}/status.json").mock(
        return_value=httpx.Response(200, json=doc("status.json") | {"store_ok": False})
    )
    await poller._refresh_relay_software([relay], {relay.slug: _ok()}, 1000.0)
    status = poller._relay_status(relay, _ok())
    assert status.state is State.DEGRADED
    assert status.reason is Reason.BAD_RESPONSE
    assert status.detail == "relay reports its message store is failing"


# ----------------------------------------------------------------------------- API


def _status(epicmail_relay: bool) -> TargetStatus:
    t = Target(
        kind=Kind.EPICBOX,
        slug="relay.example",
        endpoint="wss://relay.example:443/",
        display_host="relay.example",
        source=Source.SEED,
    )
    s = TargetStatus(target=t, state=State.HEALTHY, protocol_version="3.1.0")
    if epicmail_relay:
        s.relay_info = parse_well_known(doc("well-known.json"))
        s.relay_health = parse_status(doc("status.json"))
    return s


def test_api_row_adds_fields_and_keeps_the_old_ones() -> None:
    from epic_status.api import _serialise_target

    classic = _serialise_target(_status(False))
    em = _serialise_target(_status(True))
    # Every field a classic row had is still there, with the same meaning.
    assert set(classic) <= set(em)
    for key in ("protocol_version", "cert_expires_in_days", "ipv6", "state", "endpoint"):
        assert key in em
    assert classic["software"] is None and classic["epicmail"] is None
    assert em["software"] == "epicmail"
    detail = em["epicmail"]
    assert detail["version"] == "0.1.1"
    assert detail["cancel"] is True and detail["mux"] is True
    assert detail["https_api"] == {"protocol": "epicbox-https/0.1", "ok": None}
    assert "messages_waiting" not in detail["health"]
    assert "sockets" not in detail["health"]
    assert detail["health"]["ready"] is True


def test_api_epicmail_detail_matches_its_documented_schema() -> None:
    from epic_status import schemas
    from epic_status.api import epicmail_detail

    detail = epicmail_detail(_status(True))
    assert detail is not None
    model = schemas.EpicmailDetail.model_validate(detail)
    assert set(detail) == set(schemas.EpicmailDetail.model_fields)
    assert model.health is not None
    assert set(detail["health"]) == set(schemas.EpicmailHealth.model_fields)


def test_board_groups_relays_by_software_and_shows_the_panel() -> None:
    from datetime import UTC, datetime

    from epic_status.models import NetworkSummary, Snapshot
    from epic_status.render import render_board, render_submit

    em, classic = _status(True), _status(False)
    classic.target = classic.target.model_copy(update={"slug": "old.example", "display_host": "old.example"})
    snap = Snapshot(
        generated_at=datetime.now(UTC),
        prober_id="test",
        network=NetworkSummary(epicbox_total=2, epicbox_healthy=2),
        nodes=[],
        epicbox=[em, classic],
        pools=[],
    )
    html = render_board(snapshot=snap, settings=Settings())
    # One table in two parts: Epicmail first, then the classic relay. No per-row badge.
    assert "<h2>Transaction relays</h2>" in html
    assert "em-badge" not in html
    em_at, classic_at = html.index('class="rows rows-epicmail"'), html.index('class="rows rows-classic"')
    assert em_at < classic_at
    em_part, classic_part = html[em_at:classic_at], html[classic_at:]
    assert 'id="row-relay.example"' in em_part and 'id="row-old.example"' not in em_part
    assert 'id="row-old.example"' in classic_part
    # One column header for both parts, and one band each: name, repository, latest release.
    assert html.count('class="row-head"') == 1 and html.count('class="row-band"') == 2
    assert "<span>Epicmail</span>" in em_part and "<span>Epicbox</span>" in classic_part
    # Under an Epicmail part the classic part is marked to fold and carries its count.
    assert 'class="rows rows-classic" data-part-fold' in html
    assert "data-part-sum" in classic_part and "data-part-sum" not in em_part
    assert "<b>1</b> of 1 online" in classic_part
    assert 'href="https://github.com/BTLabsUK/epicmail/releases/tag/v' in em_part
    assert 'href="https://github.com/EpicCash/epic-epicbox-docker"' in classic_part
    assert html.count('class="rel"') == 2
    assert "recommended relay" not in html
    assert "tools/get.sh" not in html
    assert "https://github.com/BTLabsUK/epicmail" in html
    assert 'class="d-em"' in html
    assert "message store" in html
    for gone in ("waiting messages", "forward queue", "open sockets",
                 "listening mailboxes", "relay uptime", "counters read"):
        assert gone not in html
    assert "with cancel" in html
    # Counts only on the page, too.
    assert "epicmail.btlabs.uk:443" not in html

    form = render_submit(snapshot=snap, settings=Settings())
    assert 'value="epicmail"' in form and 'value="epicbox"' in form


def test_a_board_with_only_classic_relays_keeps_its_column_header() -> None:
    from datetime import UTC, datetime

    from epic_status.models import NetworkSummary, Snapshot
    from epic_status.render import render_board

    snap = Snapshot(
        generated_at=datetime.now(UTC),
        prober_id="test",
        network=NetworkSummary(
            epicbox_total=1,
            epicbox_healthy=1,
            released_epicmail_version="9.8.7",
            released_epicmail_url="https://github.com/BTLabsUK/epicmail/releases/tag/v9.8.7",
        ),
        nodes=[],
        epicbox=[_status(False)],
        pools=[],
    )
    html = render_board(snapshot=snap, settings=Settings())
    assert 'class="rows rows-epicmail"' not in html
    assert html.count('class="row-head"') == 1 and html.count('class="row-band"') == 1
    # Alone it is the whole table, so it is not folded away.
    assert "data-part-fold" not in html and "data-part-sum" not in html
    # The Epicmail release belongs to the Epicmail band, which this board does not have.
    assert "9.8.7" not in html

    snap.epicbox.append(_status(True))
    html = render_board(snapshot=snap, settings=Settings())
    assert ">9.8.7</a>" in html


def test_health_of_a_non_epicmail_relay_is_not_published() -> None:
    from epic_status.api import epicmail_detail

    s = _status(False)
    s.relay_health = RelayHealth(ok=True, store_ok=True)
    assert epicmail_detail(s) is None


def _named(host: str, epicmail_relay: bool) -> TargetStatus:
    s = _status(epicmail_relay)
    s.target = s.target.model_copy(update={"slug": host, "display_host": host})
    return s


def test_the_epicbox_name_of_a_relay_listed_twice_goes_to_the_second_part() -> None:
    from epic_status.render import relay_parts

    relays = [
        _named("epicbox.one.example", True),
        _named("epicmail.one.example", True),
        # No `epicmail.` name listed: this row is the only one the relay has.
        _named("epicbox.two.example", True),
        # Its `epicmail.` name is listed but does not run Epicmail, so it is no pair.
        _named("epicbox.three.example", True),
        _named("epicmail.three.example", False),
        _named("epicbox.classic.example", False),
    ]
    first, second = relay_parts(relays)
    assert [s.target.display_host for s in first] == [
        "epicmail.one.example",
        "epicbox.two.example",
        "epicbox.three.example",
    ]
    assert [s.target.display_host for s in second] == [
        "epicbox.one.example",
        "epicmail.three.example",
        "epicbox.classic.example",
    ]


def test_a_port_or_capitals_in_the_host_do_not_hide_a_pair() -> None:
    from epic_status.render import relay_parts

    first, second = relay_parts(
        [_named("Epicbox.One.example:443", True), _named("epicmail.one.example", True)]
    )
    assert [s.target.display_host for s in first] == ["epicmail.one.example"]
    assert [s.target.display_host for s in second] == ["Epicbox.One.example:443"]


def test_a_relay_listed_under_two_names_counts_once() -> None:
    from epic_status.models import relay_counts

    relays = [
        _named("epicmail.one.example", True),
        _named("epicbox.one.example", True),
        _named("epicbox.two.example", True),
        _named("epicbox.classic.example", False),
    ]
    assert relay_counts(relays) == (3, 3, 0, 0)


def test_a_pair_counts_in_the_state_of_the_worse_name() -> None:
    from epic_status.models import relay_counts

    first, second = _named("epicmail.one.example", True), _named("epicbox.one.example", True)
    other = _named("epicbox.classic.example", False)

    second.state = State.DOWN
    assert relay_counts([first, second, other]) == (2, 1, 0, 1)
    second.state = State.DEGRADED
    assert relay_counts([second, first, other]) == (2, 1, 1, 0)
    first.state, second.state = State.DOWN, State.HEALTHY
    assert relay_counts([first, second, other]) == (2, 1, 0, 1)
    # Not yet checked is in no bucket, as for a single row.
    first.state, second.state = State.HEALTHY, State.UNKNOWN
    assert relay_counts([first, second, other]) == (2, 1, 0, 0)
