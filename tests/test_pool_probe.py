"""Mining pool probe: one parser, a reader per pool API, one output.

Every payload in this file is a real recorded response from the pool it claims to
come from, trimmed only by dropping entries from a block list. The pools implement the
same idea with different envelopes, names and units, so synthetic fixtures here would
test the fixtures rather than the readers.

The assertions that matter most, because each is a silent failure in production:

* Both pools come out in the same shape, in the same units.
* The fee. A pool changed `"0.02"` to `"2"` and the board showed 200%. A fee that is
  not a percentage between 0 and 100 is not published.
* The last block a pool FOUND is never published as a chain height. Comparing it to the
  network tip marks small pools degraded for being small. `chain_height` is filled only
  from a field an API documents as the chain height.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from epic_status.config import Thresholds
from epic_status.models import PoolHashrate, PoolProbe, Reason, State
from epic_status.probes.pool import (
    ADAPTERS,
    PoolAdapter,
    Unreadable,
    _as_float,
    _as_int,
    _fee,
    _payout,
    _when,
    parse_pool_stats,
)
from epic_status.render import _hashrate
from epic_status.score import pool_state

# Recorded from https://epic.iniaku.my.id/api/pool/stats on 2026-11-23.
INIAKU: dict[str, Any] = {
    "status": True,
    "data": {
        "poolInfo": {"method": ["pplns", "solo"], "fee": "2", "threshold": "p1/s3"},
        "poolStats": {
            "activeUsers": 228,
            "activeWorkers": 1413,
            "hashrateRandomX": 17133466.289580908,
            "hashrateProgPow": 21590502594.64744,
            "hashrateCuckoo": 57035.44444444444,
            "hashrateWindow": "1h",
            "hashrate10mRandomX": 17879387.023510974,
            "hashrate10mProgPow": 21425468257.34052,
            "hashrate10mCuckoo": 0,
            "onlineRandomX": 833,
            "onlineProgPow": 491,
            "onlineCuckoo": 89,
            "currentHeight": 3810659,
        },
        "poolBlocks": {
            "total": 20151,
            "soloBlocks": 2430,
            "poolBlocks": 17554,
            "orphans": 167,
            "paid": 7404,
            "unpaid": 12580,
            "lastFound": 3810659,
            "latest": [
                {
                    "blockHeight": 3810659,
                    "time": 1795429645,
                    "algo": "Progpow",
                    "reward": 0.9889,
                    "hash": "f1ef1a40f077dd2a2497b1e0819be595adb5d75cc4bf128fdee33778ac53b4be",
                    "is_solo": False,
                    "maturity": "immature",
                    "paid": False,
                }
            ],
        },
    },
}

# Recorded from https://epicpool.io/api/v1/pool/summary on 2026-11-23. Flat, per
# algorithm, ISO timestamps, and `height` documented as the chain height.
EPICPOOL: dict[str, Any] = {
    "algos": {
        "cuckoo": {
            "hashrate": 0,
            "hashrate_average": 0,
            "miners": 0,
            "workers": 0,
            "blocks_24h": 0,
            "last_block": None,
            "network_hashrate": 10152001.766666668,
            "network_difficulty": 609120106,
        },
        "progpow": {
            "hashrate": 642480332.516267,
            "hashrate_average": 309500000,
            "miners": 4,
            "workers": 11,
            "blocks_24h": 12,
            "last_block": {"height": 3810348, "time": "2026-11-23T05:32:57.300358Z"},
            "network_hashrate": 29935293900.6,
            "network_difficulty": 1796117634036,
        },
        "randomx": {
            "hashrate": 653773.6058539139,
            "hashrate_average": 338833.3333333333,
            "miners": 2,
            "workers": 28,
            "blocks_24h": 15,
            "last_block": {"height": 3810597, "time": "2026-11-23T09:29:17.925242Z"},
            "network_hashrate": 20506566.183333334,
            "network_difficulty": 1230393971,
        },
    },
    "coin": "EPIC",
    "fee_percent": 2.5,
    "height": 3810661,
    "payout_scheme": "PPLNS",
    "payout_threshold": "0.10000000",
    "pool": "epicpool.io",
    "stratum": [
        {
            "region": "Europe",
            "location": "Netherlands",
            "host": "eu1.epicpool.io",
            "ports": {"plain": 3333, "tls": 3334},
        }
    ],
}

# What a pool's API host returned for a path that does not exist: a 200 with a valid
# JSON body, which is why a status code cannot be trusted on a stats route.
ENDPOINT_INDEX: dict[str, Any] = {
    "endpoints": {
        "/explorer": ["/getsupply", "/getheight"],
        "/pool": ["/getlocations", "/getstats"],
    },
    "method": "GET",
    "version": "1.1",
}

# Recorded from https://pool.minefleet.fr/api/pools on 2026-11-23, trimmed to the two Epic
# entries (ProgPoW, RandomX) and one other coin.
MINEFLEET: dict[str, Any] = {'pools': [{'id': 'epic-pplns',
            'coin': {'type': 'EPIC',
                     'name': 'Epic Cash',
                     'symbol': 'EPIC',
                     'family': 'grin',
                     'algorithm': 'ProgPoW'},
            'ports': {'3633': {'name': 'Stratum solo',
                               'protocol': 'stratum',
                               'tls': False,
                               'difficulty': 200000000,
                               'varDiff': {'minDiff': 1000000,
                                           'maxDiff': 4503599627370496,
                                           'targetTime': 15,
                                           'retargetTime': 60}},
                      '3634': {'name': 'Stratum PPLNS',
                               'protocol': 'stratum',
                               'tls': False,
                               'difficulty': 200000000,
                               'varDiff': {'minDiff': 1000000,
                                           'maxDiff': 4503599627370496,
                                           'targetTime': 15,
                                           'retargetTime': 60}}},
            'paymentProcessing': {'enabled': True,
                                  'minimumPayment': 0.1,
                                  'payoutScheme': 'PPLNS'},
            'poolFeePercent': 1.5,
            'poolStats': {'connectedMiners': 87,
                          'connectedWorkers': 87,
                          'poolHashrate': 7503197641.891985,
                          'sharesPerSecond': 5.739030646753883},
            'networkStats': {'networkType': 'Main',
                             'rewardType': 'POW',
                             'blockHeight': 3811150,
                             'networkDifficulty': 1757668464457,
                             'networkHashrate': 29294474407.616665,
                             'connectedPeers': 24,
                             'lastNetworkBlockTime': '2026-11-23T18:30:44.000Z'},
            'totalBlocks': 1657,
            'lastPoolBlockTime': '2026-11-23T18:28:39.831Z',
            'poolEffort': 0.18559942387523987},
           {'id': 'epic-cpu-pplns',
            'coin': {'type': 'EPIC',
                     'name': 'Epic Cash',
                     'symbol': 'EPIC',
                     'family': 'grin',
                     'algorithm': 'RandomX'},
            'ports': {'3635': {'name': 'Stratum solo',
                               'protocol': 'stratum',
                               'tls': False,
                               'difficulty': 16384,
                               'varDiff': {'minDiff': 5000,
                                           'maxDiff': 17592186044416,
                                           'targetTime': 30,
                                           'retargetTime': 60}},
                      '3636': {'name': 'Stratum PPLNS',
                               'protocol': 'stratum',
                               'tls': False,
                               'difficulty': 16384,
                               'varDiff': {'minDiff': 5000,
                                           'maxDiff': 17592186044416,
                                           'targetTime': 30,
                                           'retargetTime': 60}}},
            'paymentProcessing': {'enabled': True,
                                  'minimumPayment': 0.1,
                                  'payoutScheme': 'PPLNS'},
            'poolFeePercent': 1.5,
            'poolStats': {'connectedMiners': 12,
                          'connectedWorkers': 12,
                          'poolHashrate': 92027.89583695031,
                          'sharesPerSecond': 0.43807201843547516},
            'networkStats': {'networkType': 'Main',
                             'rewardType': 'POW',
                             'blockHeight': 3811150,
                             'networkDifficulty': 1359039761,
                             'networkHashrate': 22650662.683333334,
                             'connectedPeers': 24,
                             'lastNetworkBlockTime': '2026-11-23T18:30:44.000Z'},
            'totalBlocks': 31,
            'lastPoolBlockTime': '2026-11-23T16:05:33.491Z',
            'poolEffort': 0.28471515660780045},
           {'id': 'btc-pplns',
            'coin': {'type': 'BTC',
                     'name': 'Bitcoin',
                     'symbol': 'BTC',
                     'family': 'bitcoin',
                     'algorithm': 'SHA-256'},
            'ports': {'3833': {'name': 'Stratum solo',
                               'protocol': 'stratum',
                               'tls': False,
                               'difficulty': 1024,
                               'varDiff': {'minDiff': 1,
                                           'maxDiff': 1000000000000,
                                           'targetTime': 10,
                                           'retargetTime': 30}},
                      '3834': {'name': 'Stratum PPLNS',
                               'protocol': 'stratum',
                               'tls': False,
                               'difficulty': 1024,
                               'varDiff': {'minDiff': 1,
                                           'maxDiff': 1000000000000,
                                           'targetTime': 10,
                                           'retargetTime': 30}}},
            'paymentProcessing': {'enabled': True,
                                  'minimumPayment': 0.0001,
                                  'payoutScheme': 'PPLNS'},
            'poolFeePercent': 2,
            'poolStats': {'connectedMiners': 6,
                          'connectedWorkers': 6,
                          'poolHashrate': 8202532342028.086,
                          'sharesPerSecond': 0.5982048795753938},
            'networkStats': {'networkType': 'Main',
                             'rewardType': 'POW',
                             'blockHeight': 977250,
                             'networkDifficulty': 134823837323272.66,
                             'networkHashrate': 1.180275786954413e+21,
                             'connectedPeers': 10,
                             'lastNetworkBlockTime': '2026-11-23T18:18:53.618Z'},
            'totalBlocks': 0,
            'poolEffort': 5.911120880568752e-12}],
 'generatedAt': '2026-11-23T18:33:07.282Z'}

# Recorded from /api/pools/epic-pplns/blocks?page=0&pageSize=5 on 2026-11-23 (one block kept
# per list; the newest first, as the pool returns them).
MINEFLEET_BLOCKS_PROGPOW: list[dict[str, Any]] = [
    {
        "poolId": "epic-pplns",
        "blockHeight": 3811146,
        "status": "pending",
        "confirmationProgress": 0.003,
        "miner": "esZo3x...m924",
        "created": "2026-11-23T18:28:39.831Z",
        "effort": 1.27,
    }
]
MINEFLEET_BLOCKS_RANDOMX: list[dict[str, Any]] = [
    {
        "poolId": "epic-cpu-pplns",
        "blockHeight": 3811100,
        "status": "confirmed",
        "confirmationProgress": 1,
        "miner": "esZo3x...m924",
        "created": "2026-11-23T17:50:00.000Z",
        "effort": 0.8,
    }
]

PAYLOADS = {"iniaku": INIAKU, "epicpool": EPICPOOL, "minefleet": MINEFLEET}


# ------------------------------------------------------------------ real payloads


def test_iniaku_payload_parses() -> None:
    probe = parse_pool_stats(INIAKU, ADAPTERS["iniaku"])
    assert probe.ok
    assert probe.reason is Reason.OK
    assert probe.miners == 228
    assert probe.workers == 1413
    assert probe.hashrate == PoolHashrate(
        randomx=17133466.289580908, progpow=21590502594.64744, cuckoo=57035.44444444444
    )
    assert probe.fee_percent == pytest.approx(2.0)
    assert probe.payout == ["PPLNS", "SOLO"]
    assert probe.last_block_height == 3810659
    assert probe.last_block_at == datetime(2026, 11, 23, 10, 27, 25, tzinfo=UTC)


def test_epicpool_payload_parses() -> None:
    probe = parse_pool_stats(EPICPOOL, ADAPTERS["epicpool"])
    assert probe.ok
    assert probe.reason is Reason.OK
    # Summed across algorithms: the summary has no pool-wide count.
    assert probe.miners == 6
    assert probe.workers == 39
    # The on-turn figure, not `hashrate_average`.
    assert probe.hashrate == PoolHashrate(
        randomx=653773.6058539139, progpow=642480332.516267, cuckoo=0.0
    )
    assert probe.fee_percent == pytest.approx(2.5)
    assert probe.payout == ["PPLNS"]
    # The latest block of any algorithm, with that block's own time.
    assert probe.last_block_height == 3810597
    assert probe.last_block_at == datetime(2026, 11, 23, 9, 29, 17, 925242, tzinfo=UTC)


def test_minefleet_payload_parses() -> None:
    probe = parse_pool_stats(MINEFLEET, ADAPTERS["minefleet"])
    assert probe.ok
    assert probe.miners == 87 + MINEFLEET["pools"][1]["poolStats"]["connectedMiners"]
    assert probe.hashrate is not None
    assert probe.hashrate.progpow == pytest.approx(7503197641.891985)
    assert probe.hashrate.randomx == MINEFLEET["pools"][1]["poolStats"]["poolHashrate"]
    assert probe.hashrate.cuckoo is None
    assert probe.fee_percent == pytest.approx(1.5)
    assert probe.payout == ["PPLNS"]
    assert probe.chain_height == 3811150
    assert probe.last_block_at == datetime(2026, 11, 23, 18, 28, 39, 831000, tzinfo=UTC)
    # Not in /api/pools: filled by the enrich step.
    assert probe.last_block_height is None


def test_minefleet_without_an_epic_entry_is_unreadable() -> None:
    payload = {"pools": [p for p in MINEFLEET["pools"] if p["coin"]["symbol"] != "EPIC"]}
    probe = parse_pool_stats(payload, ADAPTERS["minefleet"])
    assert not probe.ok
    assert probe.reason is Reason.BAD_RESPONSE
    assert "EPIC" in (probe.error or "")


def test_minefleet_enrich_takes_the_newest_block_and_skips_orphans() -> None:
    adapter = ADAPTERS["minefleet"]
    base = parse_pool_stats(MINEFLEET, adapter)
    orphan = {**MINEFLEET_BLOCKS_PROGPOW[0], "blockHeight": 3811200, "status": "orphaned"}
    assert adapter.enrich is not None
    probe = adapter.enrich(base, [[orphan, *MINEFLEET_BLOCKS_PROGPOW], MINEFLEET_BLOCKS_RANDOMX])
    assert probe.last_block_height == 3811146
    assert probe.last_block_at == datetime(2026, 11, 23, 18, 28, 39, 831000, tzinfo=UTC)


def test_minefleet_enrich_with_failed_fetches_keeps_the_probe() -> None:
    adapter = ADAPTERS["minefleet"]
    base = parse_pool_stats(MINEFLEET, adapter)
    assert adapter.enrich is not None
    assert adapter.enrich(base, [None, {"not": "a list"}]) == base


@pytest.mark.parametrize("fail", [False, True])
def test_probe_pool_fetches_the_extras_and_survives_their_failure(fail: bool) -> None:
    import asyncio

    import httpx

    from epic_status.net import Resolved
    from epic_status.probes.pool import probe_pool

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        if request.url.path == "/api/pools":
            return httpx.Response(200, json=MINEFLEET)
        if fail:
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(200, json=MINEFLEET_BLOCKS_PROGPOW)

    async def run() -> PoolProbe:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolved = Resolved(
                hostname="pool.example", port=443, addresses=("203.0.113.9",), scheme="https"
            )
            return await probe_pool(client, resolved, "minefleet")

    probe = asyncio.run(run())
    assert probe.ok
    assert seen[1:] == [
        "/api/pools/epic-pplns/blocks?page=0&pageSize=5",
        "/api/pools/epic-cpu-pplns/blocks?page=0&pageSize=5",
    ]
    assert probe.last_block_height == (None if fail else 3811146)


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_every_reader_fills_the_same_output(name: str) -> None:
    """THE POINT of the parser: whichever API answered, the board is fed one shape."""
    probe = parse_pool_stats(PAYLOADS[name], ADAPTERS[name])
    assert probe.ok
    if ADAPTERS[name].enrich is not None:
        probe = ADAPTERS[name].enrich(probe, [MINEFLEET_BLOCKS_PROGPOW, MINEFLEET_BLOCKS_RANDOMX])
    assert isinstance(probe.miners, int)
    assert isinstance(probe.workers, int)
    assert probe.hashrate is not None
    assert probe.hashrate.randomx is not None and probe.hashrate.progpow is not None
    assert probe.fee_percent is not None and 0 <= probe.fee_percent <= 100
    assert probe.payout and all(p.isupper() for p in probe.payout)
    assert isinstance(probe.last_block_height, int)
    assert probe.last_block_at is not None
    # Naive datetimes silently compare wrong against datetime.now(UTC).
    assert probe.last_block_at.tzinfo is not None


def test_every_adapter_has_a_recorded_payload() -> None:
    """A reader without a real response under test is a reader nobody has checked."""
    assert set(PAYLOADS) == set(ADAPTERS)


# ------------------------------------------------------------------------ the fee


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2", 2.0), (2.5, 2.5), ("0", 0.0), (100, 100.0), (200, None), (-1, None), ("x", None)],
)
def test_a_fee_outside_a_percentage_is_not_published(
    raw: Any, expected: float | None
) -> None:
    assert _fee(raw) == expected


def test_the_fee_that_read_two_hundred_percent() -> None:
    """iniaku published `"0.02"` and was multiplied by a hundred; then it published
    `"2"` and the board said 200%. It is read as the percentage it now is."""
    assert parse_pool_stats(INIAKU, ADAPTERS["iniaku"]).fee_percent == 2.0


# -------------------------------------------------------- the height misreading


def test_pool_probe_publishes_no_height_under_a_nodes_name() -> None:
    """A pool's numbers never land in the fields a node's chain tip lives in.

    An earlier version of this module exposed `currentHeight` as `height`, the poller
    copied it into the field a node's tip lives in, and the board marked a healthy
    nine-miner pool as lagging the tip.
    """
    assert "height" not in PoolProbe.model_fields
    assert "tip_lag" not in PoolProbe.model_fields


def test_current_height_is_required_but_never_a_chain_height() -> None:
    # Required, because its presence is what tells a stats document from any other
    # JSON object the host answers with...
    stripped = {
        "status": True,
        "data": {
            **INIAKU["data"],
            "poolStats": {
                k: v for k, v in INIAKU["data"]["poolStats"].items() if k != "currentHeight"
            },
        },
    }
    assert not parse_pool_stats(stripped, ADAPTERS["iniaku"]).ok

    # ...but it is the last block FOUND, so it is not the pool node's tip.
    probe = parse_pool_stats(INIAKU, ADAPTERS["iniaku"])
    assert probe.last_block_height == INIAKU["data"]["poolBlocks"]["lastFound"]
    assert probe.chain_height is None


def test_chain_height_comes_only_from_a_documented_chain_height() -> None:
    probe = parse_pool_stats(EPICPOOL, ADAPTERS["epicpool"])
    assert probe.chain_height == 3810661
    # And it is not the last block found, which is 64 blocks earlier.
    assert probe.last_block_height == 3810597


# ------------------------------------------------------------------ the 200 trap


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_an_endpoint_index_is_not_a_healthy_pool(name: str) -> None:
    """A 200 proves nothing. A typo in an adapter path would otherwise read as a working
    pool with every metric null."""
    probe = parse_pool_stats(ENDPOINT_INDEX, ADAPTERS[name])
    assert not probe.ok
    assert probe.reason is Reason.BAD_RESPONSE
    assert probe.error


def test_each_reader_rejects_the_other_pool_s_payload() -> None:
    """Applying the wrong adapter must fail loudly rather than half-parse."""
    assert not parse_pool_stats(INIAKU, ADAPTERS["epicpool"]).ok
    assert not parse_pool_stats(EPICPOOL, ADAPTERS["iniaku"]).ok


def test_iniaku_requires_status_true() -> None:
    payload = {"status": False, "data": INIAKU["data"]}
    probe = parse_pool_stats(payload, ADAPTERS["iniaku"])
    assert not probe.ok
    assert "status false" in (probe.error or "")


@pytest.mark.parametrize("name", sorted(ADAPTERS))
@pytest.mark.parametrize(
    "payload",
    [None, [], "text", 0, {}, {"data": None}, {"status": True}, {"algos": []}, {"algos": {}}],
)
def test_garbage_never_raises(name: str, payload: Any) -> None:
    probe = parse_pool_stats(payload, ADAPTERS[name])
    assert not probe.ok
    assert probe.error


def test_a_reader_that_trips_is_a_bad_response_not_a_crash() -> None:
    """The sweep must survive a reader meeting a shape nobody anticipated."""

    def trips(payload: dict[str, Any]) -> PoolProbe:
        return PoolProbe(ok=True, miners=payload["missing"]["deeper"])

    probe = parse_pool_stats({"a": 1}, PoolAdapter(path="/x", read=trips))
    assert not probe.ok
    assert probe.reason is Reason.BAD_RESPONSE
    assert "KeyError" in (probe.error or "")


def test_unreadable_carries_its_message_to_the_row() -> None:
    def refuses(payload: dict[str, Any]) -> PoolProbe:
        raise Unreadable("no stats here")

    probe = parse_pool_stats({"a": 1}, PoolAdapter(path="/x", read=refuses))
    assert probe.error == "no stats here"


# ------------------------------------------------------------------- coercion


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (5, 5),
        (5.9, 5),
        ("5", 5),
        ("5.9", 5),
        ("", None),
        ("x", None),
        (None, None),
        # True is an int in Python and would read as the number 1.
        (True, None),
        ([], None),
        (float("nan"), None),
        ("inf", None),
    ],
)
def test_as_int(raw: Any, expected: int | None) -> None:
    assert _as_int(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(2.5, 2.5), (2, 2.0), ("0.02", 0.02), ("x", None), (None, None), (True, None)],
)
def test_as_float(raw: Any, expected: float | None) -> None:
    assert _as_float(raw) == expected


def test_quoted_numbers_are_accepted() -> None:
    """These APIs are inconsistent about quoting, and a quoted count is not a fault."""
    payload = {
        "status": True,
        "data": {
            "poolInfo": {"fee": "2.5"},
            "poolStats": {
                "activeUsers": "9",
                "activeWorkers": "25",
                "currentHeight": "100",
                "hashrateRandomX": "1500.5",
            },
            "poolBlocks": {"lastFound": "100", "latest": []},
        },
    }
    probe = parse_pool_stats(payload, ADAPTERS["iniaku"])
    assert probe.ok
    assert (probe.miners, probe.workers, probe.last_block_height) == (9, 25, 100)
    assert probe.hashrate is not None and probe.hashrate.randomx == 1500.5


def test_missing_optional_sections_still_parse() -> None:
    """A pool that publishes stats but no block list is degraded data, not an outage."""
    probe = parse_pool_stats(
        {"status": True, "data": {"poolStats": {"currentHeight": 100}}}, ADAPTERS["iniaku"]
    )
    assert probe.ok
    assert probe.fee_percent is None
    assert probe.last_block_at is None
    assert probe.last_block_height == 100
    # No hashrate published is None, not three empty figures and not zero.
    assert probe.hashrate is None
    assert probe.payout == []


def test_an_algorithm_the_pool_does_not_list_is_none_not_zero() -> None:
    payload = {**EPICPOOL, "algos": {"randomx": EPICPOOL["algos"]["randomx"]}}
    probe = parse_pool_stats(payload, ADAPTERS["epicpool"])
    assert probe.ok
    assert probe.hashrate == PoolHashrate(randomx=653773.6058539139)
    assert (probe.miners, probe.workers) == (2, 28)


@pytest.mark.parametrize("rate", [-1, float("nan"), float("inf"), "x", None, True])
def test_an_absurd_hashrate_is_dropped(rate: Any) -> None:
    payload = {
        "status": True,
        "data": {"poolStats": {"currentHeight": 100, "hashrateProgPow": rate}},
    }
    probe = parse_pool_stats(payload, ADAPTERS["iniaku"])
    assert probe.ok
    assert probe.hashrate is None


@pytest.mark.parametrize("stamp", [0, -1, 10**18, "soon", "", None, [], True])
def test_absurd_block_times_become_none(stamp: Any) -> None:
    assert _when(stamp) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1795429645, datetime(2026, 11, 23, 10, 27, 25, tzinfo=UTC)),
        ("1795429645", datetime(2026, 11, 23, 10, 27, 25, tzinfo=UTC)),
        ("2026-11-23T08:35:19.9082Z", datetime(2026, 11, 23, 8, 35, 19, 908200, tzinfo=UTC)),
        # No offset is read as UTC rather than left naive.
        ("2026-11-23T08:35:19", datetime(2026, 11, 23, 8, 35, 19, tzinfo=UTC)),
    ],
)
def test_block_times_in_either_form(raw: Any, expected: datetime) -> None:
    assert _when(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["pplns", "solo"], ["PPLNS", "SOLO"]),
        ("PPLNS", ["PPLNS"]),
        (["pplns", "PPLNS", " solo "], ["PPLNS", "SOLO"]),
        (None, []),
        ([1, None, ""], []),
        # A free-text field is not a scheme name.
        (["a" * 40], []),
    ],
)
def test_payout_schemes(raw: Any, expected: list[str]) -> None:
    assert _payout(raw) == expected


# -------------------------------------------------------------------- adapters


def test_every_adapter_has_a_rooted_path() -> None:
    for name, adapter in ADAPTERS.items():
        assert isinstance(adapter, PoolAdapter), name
        assert adapter.path.startswith("/"), name
        assert not adapter.path.endswith("/"), name
        assert callable(adapter.read), name


def test_adapter_names_are_stable_identifiers() -> None:
    """They are written into the database and the committed registry, so a rename is
    a migration rather than a tidy-up."""
    for name in ADAPTERS:
        assert name.islower()
        assert name.isidentifier()


# ------------------------------------------------------------- hashrate as text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, "—"),
        (0, "0"),
        (654.2, "654 H/s"),
        (653773.6, "654 kH/s"),
        (17133466.3, "17.1 MH/s"),
        (1012560000, "1.01 GH/s"),
        (21590502594.6, "21.6 GH/s"),
        # Rounds up into the next prefix instead of printing 1000 k.
        (999_600, "1.00 MH/s"),
        (99_960, "100 kH/s"),
    ],
)
def test_hashrate_is_three_figures_and_a_prefix(value: float | None, text: str) -> None:
    assert _hashrate(value) == text
    # Nine characters is what the table's two hashrate columns are sized for.
    assert len(text) <= 9


def test_cuckoo_hashrate_names_its_own_unit() -> None:
    assert _hashrate(57035.4, "Gps") == "57.0 kGps"


# ----------------------------------------------------------------- the verdict


def test_a_reachable_pool_is_healthy() -> None:
    state, reason, detail = pool_state(
        parse_pool_stats(EPICPOOL, ADAPTERS["epicpool"]),
        consecutive_failures=0,
        thresholds=Thresholds(),
    )
    assert (state, reason, detail) == (State.HEALTHY, Reason.OK, None)


def test_a_quiet_pool_is_not_penalised() -> None:
    """The regression guard for the whole design.

    epicpool's last block was an hour old while iniaku had found one minutes earlier.
    Both were working. Anything that degrades the first is wrong.
    """
    probe = parse_pool_stats(EPICPOOL, ADAPTERS["epicpool"])
    state, reason, _ = pool_state(
        probe, consecutive_failures=0, thresholds=Thresholds()
    )
    assert state is State.HEALTHY
    assert reason is not Reason.TIP_LAG


def test_failures_are_confirmed_before_being_called_down() -> None:
    th = Thresholds()
    probe = PoolProbe(ok=False, reason=Reason.TIMEOUT, error="timed out")
    assert (
        pool_state(probe, consecutive_failures=1, thresholds=th)[0] is State.DEGRADED
    )
    assert (
        pool_state(
            probe, consecutive_failures=th.down_after_failures, thresholds=th
        )[0]
        is State.DOWN
    )
