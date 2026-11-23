"""Mining pool probes: one parser, one output, a reader per pool API.

Pools are the one monitored kind with no protocol. A node speaks Epic's JSON-RPC
and a relay speaks the Epicbox WebSocket handshake, so one probe covers every host
of that kind forever. A pool exposes whatever its author wrote: different path,
different envelope, different names and units for the same number.

So the work is split in two, and only one half knows about any particular pool:

* `parse_pool_stats` is the parser. It takes a decoded response and the adapter named
  in the registry, and returns a `PoolProbe` — the same fields, in the same units, for
  every pool. The request, the status codes, the rules about what a believable value
  is and everything after the parser (scoring, the board, the API) are shared.
* A **reader** is the half that knows one API. It is a function from that API's JSON
  to a `PoolProbe`, registered in `ADAPTERS` under the name the registry uses.

Adding a pool
-------------
1. Record a real response and put it in tests/test_pool_probe.py.
2. Write `_read_<name>(payload) -> PoolProbe`. Use the helpers below for every value
   (`_count`, `_rate`, `_when`, `_payout`), raise `Unreadable` when the response is not
   the stats document, and leave a field None when the API does not publish it.
3. Add `"<name>": PoolAdapter(path=..., read=_read_<name>)` to `ADAPTERS`. An API whose
   main document lacks a field another route has may add `extra_paths` and an `enrich`
   function (see `minefleet`); a failed extra fetch never makes a good probe bad.
4. Add the `[[pool]]` entry to config/targets.toml with `adapter = "<name>"`.

Nothing else changes: the row, the panel and status.json are fed from `PoolProbe`.

The output, and what each field means
-------------------------------------
`miners` and `workers` are connected accounts and rigs. `hashrate` is one figure per
algorithm, on that algorithm's own turns, in H/s (Cuckoo: graphs/s); there is no total,
because the three are not the same unit. `fee_percent` is a percentage. `payout` is the
payout schemes the pool names. `last_block_height` and `last_block_at` are the last
block the pool FOUND. `chain_height` is the tip of the pool's own node.

Those last two are different numbers and one of the APIs names the first like the
second. iniaku's `poolStats.currentHeight` is identical to `poolBlocks.lastFound` and to
`latest[0].blockHeight`, every time it has been sampled: it is the height of the last
block the pool found. Measured against a live tip of 3721265, a nine-miner pool reported
3721246 while entirely healthy, having simply not won a block for eighteen minutes.

So the last block found must never be compared to the chain tip: the expected gap
scales inversely with the pool's share, and doing so puts a red light on a pool for
the crime of being small. What is honest is its AGE, reported as a fact and not as a
verdict. `chain_height` is filled only by a reader whose API documents the field as the
chain height (epicpool's `height`), and is left None otherwise.

A status code proves nothing about a stats route: one pool's API host used to answer
200 with an endpoint index for any path. Every reader therefore requires the fields
that make the response a stats document before the probe is called good.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ..models import PoolHashrate, PoolProbe, Reason
from ..net import Resolved, classify_http_error, pinned_url


class Unreadable(Exception):
    """The response is not the stats document this reader knows. The message is shown
    on the row, so it says what was missing."""


@dataclass(frozen=True)
class PoolAdapter:
    """One pool API: where its stats are, and the function that reads them."""

    path: str
    read: Callable[[dict[str, Any]], PoolProbe]
    # Optional second requests, made only after the main probe is good. `enrich` gets
    # the probe and one decoded body per path, None where that fetch or decode failed,
    # and returns the probe with whatever it could add.
    extra_paths: tuple[str, ...] = ()
    enrich: Callable[[PoolProbe, list[Any]], PoolProbe] | None = None


# ---------------------------------------------------------------------- helpers
#
# Every value a reader takes from a response goes through one of these, so a quoted
# number, a negative count or a NaN is handled once, the same way, for every pool.


def _as_int(value: Any) -> int | None:
    # Accepts the string forms too: these APIs are not consistent about quoting
    # numbers, and a quoted integer is not a reason to call a pool broken.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if math.isfinite(number) else None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _count(value: Any) -> int | None:
    """Miners, workers, a block height: a whole number that cannot be negative."""
    number = _as_int(value)
    return number if number is not None and number >= 0 else None


def _rate(value: Any) -> float | None:
    """A hashrate. Zero is a value: an algorithm nobody mines on this pool."""
    number = _as_float(value)
    if number is None or not math.isfinite(number) or number < 0:
        return None
    return number


def _fee(value: Any) -> float | None:
    """A fee already in percent. Outside 0-100 it is not a fee, whatever the unit was,
    and nothing is published rather than a number that is wrong by a factor of a hundred
    — the `200.00%` an API produced the day it changed `"0.02"` to `"2"`."""
    number = _as_float(value)
    if number is None or not math.isfinite(number) or not 0 <= number <= 100:
        return None
    return number


def _when(value: Any) -> datetime | None:
    """A block time: Unix seconds, or an ISO 8601 string. Always timezone-aware, since a
    naive datetime silently compares wrong against `datetime.now(UTC)`."""
    stamp = _as_int(value)
    if stamp is None and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    if stamp is None or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _payout(value: Any) -> list[str]:
    """Payout schemes as the pool names them, upper-cased, in the pool's order."""
    names = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    seen: list[str] = []
    for name in names:
        if isinstance(name, str) and name.strip() and len(name) <= 16:
            label = name.strip().upper()
            if label not in seen:
                seen.append(label)
    return seen


def _section(root: dict[str, Any], key: str) -> dict[str, Any]:
    value = root.get(key)
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------- readers


def _read_iniaku(payload: dict[str, Any]) -> PoolProbe:
    """epic.iniaku.my.id, `GET /api/pool/stats`.

    `{"status": true, "data": {"poolInfo", "poolStats", "poolBlocks"}}`. The envelope is
    checked rather than tolerated: silently accepting the bare object too would hide the
    day the pool changes it.
    """
    if payload.get("status") is not True:
        raise Unreadable("API reported status false")
    root = payload.get("data")
    if not isinstance(root, dict):
        raise Unreadable("no stats object in response")
    stats = root.get("poolStats")
    if not isinstance(stats, dict):
        raise Unreadable("no poolStats in response (wrong path?)")

    # `currentHeight` is the last block FOUND under a misleading name (see the module
    # note), so it is never published as a chain height. Requiring it is what tells a
    # stats document from any other JSON object the host might answer with.
    found_height = _count(stats.get("currentHeight"))
    if found_height is None:
        raise Unreadable("poolStats carried no currentHeight")

    info = _section(root, "poolInfo")
    blocks = _section(root, "poolBlocks")

    last_at: datetime | None = None
    latest = blocks.get("latest")
    if isinstance(latest, list) and latest and isinstance(latest[0], dict):
        last_at = _when(latest[0].get("time"))

    return PoolProbe(
        ok=True,
        miners=_count(stats.get("activeUsers")),
        workers=_count(stats.get("activeWorkers")),
        # The one-hour figures. The API also carries `hashrate10m*`, which reads zero for
        # an algorithm whose turn has not come up in ten minutes.
        hashrate=PoolHashrate(
            randomx=_rate(stats.get("hashrateRandomX")),
            progpow=_rate(stats.get("hashrateProgPow")),
            cuckoo=_rate(stats.get("hashrateCuckoo")),
        ),
        # Percent, as the string "2". It was the fraction "0.02" until November 2026.
        fee_percent=_fee(info.get("fee")),
        payout=_payout(info.get("method")),
        # `lastFound` and `currentHeight` are the same number; prefer the honestly named
        # one and fall back to the other.
        last_block_height=_count(blocks.get("lastFound")) or found_height,
        last_block_at=last_at,
    )


def _read_epicpool(payload: dict[str, Any]) -> PoolProbe:
    """epicpool.io, `GET /api/v1/pool/summary` — its "flat and stable" document for
    aggregators, described in https://epicpool.io/api/v1/openapi.json.

    Everything is per algorithm under `algos`. `hashrate` there is the on-turn figure
    (the one a miner sees); `hashrate_average` includes the other algorithms' turns and
    is not used. `height` is documented as the chain height, so it is the one
    `chain_height` this module publishes.
    """
    algos = payload.get("algos")
    if not isinstance(algos, dict) or not algos:
        raise Unreadable("no algos in response (wrong path?)")
    chain_height = _count(payload.get("height"))
    if chain_height is None:
        raise Unreadable("summary carried no height")

    per_algo = {
        str(name).lower(): entry for name, entry in algos.items() if isinstance(entry, dict)
    }

    def total(key: str) -> int | None:
        values = [_count(entry.get(key)) for entry in per_algo.values()]
        known = [v for v in values if v is not None]
        # An account mining two algorithms is counted under both; the summary has no
        # pool-wide figure, and for workers the sum is exact.
        return sum(known) if known else None

    last_height: int | None = None
    last_at: datetime | None = None
    for entry in per_algo.values():
        block = entry.get("last_block")
        if not isinstance(block, dict):
            continue
        height = _count(block.get("height"))
        if height is not None and (last_height is None or height > last_height):
            last_height, last_at = height, _when(block.get("time"))

    def rate(name: str) -> float | None:
        return _rate(per_algo[name].get("hashrate")) if name in per_algo else None

    return PoolProbe(
        ok=True,
        miners=total("miners"),
        workers=total("workers"),
        hashrate=PoolHashrate(
            randomx=rate("randomx"), progpow=rate("progpow"), cuckoo=rate("cuckoo")
        ),
        fee_percent=_fee(payload.get("fee_percent")),
        payout=_payout(payload.get("payout_scheme")),
        last_block_height=last_height,
        last_block_at=last_at,
        chain_height=chain_height,
    )


def _read_minefleet(payload: dict[str, Any]) -> PoolProbe:
    """pool.minefleet.fr, `GET /api/pools` (Miningcore-style, cached 30 s).

    `{"pools": [...]}` holds every coin the pool mines. Epic Cash is two entries, one
    per algorithm, told apart by `coin.algorithm` (ProgPoW, RandomX); there is no Cuckoo.
    `networkStats.blockHeight` is the node's chain height. The list carries no height of
    the last block found: `_enrich_minefleet` adds it from the blocks routes.
    """
    pools = payload.get("pools")
    if not isinstance(pools, list):
        raise Unreadable("no pools in response (wrong path?)")
    entries = [
        p
        for p in pools
        if isinstance(p, dict) and _section(p, "coin").get("symbol") == "EPIC"
    ]
    if not entries:
        raise Unreadable("no EPIC pool in response")

    def total(section: str, key: str) -> int | None:
        known = [v for p in entries if (v := _count(_section(p, section).get(key))) is not None]
        return sum(known) if known else None

    rates: dict[str, float | None] = {}
    for p in entries:
        algo = _section(p, "coin").get("algorithm")
        if isinstance(algo, str):
            rates[algo.strip().lower()] = _rate(_section(p, "poolStats").get("poolHashrate"))

    fees = [f for p in entries if (f := _fee(p.get("poolFeePercent"))) is not None]
    schemes: list[str] = []
    for p in entries:
        for name in _payout(_section(p, "paymentProcessing").get("payoutScheme")):
            if name not in schemes:
                schemes.append(name)
    heights = [
        h for p in entries if (h := _count(_section(p, "networkStats").get("blockHeight"))) is not None
    ]
    times = [t for p in entries if (t := _when(p.get("lastPoolBlockTime"))) is not None]

    return PoolProbe(
        ok=True,
        miners=total("poolStats", "connectedMiners"),
        workers=total("poolStats", "connectedWorkers"),
        hashrate=PoolHashrate(
            randomx=rates.get("randomx"), progpow=rates.get("progpow"), cuckoo=None
        ),
        fee_percent=max(fees) if fees else None,
        payout=schemes,
        last_block_at=max(times) if times else None,
        chain_height=max(heights) if heights else None,
    )


def _enrich_minefleet(probe: PoolProbe, bodies: list[Any]) -> PoolProbe:
    """Height of the newest block either Epic pool found, from `/blocks` (newest first).
    Orphaned blocks are not blocks the pool found. A failed fetch leaves the probe as is."""
    best: tuple[int, datetime | None] | None = None
    for body in bodies:
        if not isinstance(body, list):
            continue
        for block in body:
            if not isinstance(block, dict) or block.get("status") == "orphaned":
                continue
            height = _count(block.get("blockHeight"))
            if height is not None and (best is None or height > best[0]):
                best = (height, _when(block.get("created")))
    if best is None:
        return probe
    update: dict[str, Any] = {"last_block_height": best[0]}
    if best[1] is not None:
        update["last_block_at"] = best[1]
    return probe.model_copy(update=update)


ADAPTERS: dict[str, PoolAdapter] = {
    "iniaku": PoolAdapter(path="/api/pool/stats", read=_read_iniaku),
    "epicpool": PoolAdapter(path="/api/v1/pool/summary", read=_read_epicpool),
    "minefleet": PoolAdapter(
        path="/api/pools",
        read=_read_minefleet,
        extra_paths=(
            "/api/pools/epic-pplns/blocks?page=0&pageSize=5",
            "/api/pools/epic-cpu-pplns/blocks?page=0&pageSize=5",
        ),
        enrich=_enrich_minefleet,
    ),
}


# ----------------------------------------------------------------------- parser


def _unreadable(message: str) -> PoolProbe:
    return PoolProbe(ok=False, reason=Reason.BAD_RESPONSE, error=message)


def parse_pool_stats(payload: Any, adapter: PoolAdapter) -> PoolProbe:
    """Turn one stats response into the shared probe result.

    Separate from the request so every reader can be tested against a real recorded
    payload without a network call. Never raises: a reader that trips over a shape it
    did not expect is a bad response from that pool, not a crash in the sweep.
    """
    if not isinstance(payload, dict):
        return _unreadable("response was not an object")
    try:
        probe = adapter.read(payload)
    except Unreadable as exc:
        return _unreadable(str(exc))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _unreadable(f"stats in an unexpected shape ({type(exc).__name__})")
    if probe.hashrate is not None and probe.hashrate.is_empty:
        probe = probe.model_copy(update={"hashrate": None})
    return probe


async def probe_pool(
    client: httpx.AsyncClient,
    resolved: Resolved,
    adapter_name: str | None,
    *,
    timeout: float | None = None,
) -> PoolProbe:
    """One GET against the pool's stats endpoint.

    No TLS retry, unlike the node probe. A node with a bad certificate is still
    serving the chain and is worth reporting as reachable; a pool with a bad
    certificate is a pool whose miners cannot reach it either, so there is nothing
    to be generous about.
    """
    adapter = ADAPTERS.get(adapter_name or "")
    if adapter is None:
        # A registry entry naming an adapter that does not exist is a deployment
        # mistake, and it must not read as the pool being down.
        return PoolProbe(
            ok=False,
            reason=Reason.BAD_RESPONSE,
            error=f"no adapter named {adapter_name!r}",
        )

    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        response = await client.get(
            pinned_url(resolved, adapter.path),
            headers={"host": resolved.hostname, "accept": "application/json"},
            extensions={"sni_hostname": resolved.hostname},
            # Its own budget, not the node probe's. A pool's stats route is a web app
            # computing an aggregate, often behind a CDN, and a slow answer is still an
            # answer: one pool's successes ran 2.5 s on the median and 5.6 s at worst,
            # so a 4 s limit was recording one check in five as an outage.
            timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
        )
    except Exception as exc:  # noqa: BLE001 - every failure mode becomes a Reason
        reason, message = classify_http_error(exc)
        return PoolProbe(ok=False, reason=reason, error=message)

    latency_ms = int((loop.time() - start) * 1000)

    if response.status_code == 429:
        return PoolProbe(
            ok=False,
            latency_ms=latency_ms,
            http_status=429,
            reason=Reason.RATE_LIMITED,
            error="rate limited",
        )
    if response.status_code >= 400:
        return PoolProbe(
            ok=False,
            latency_ms=latency_ms,
            http_status=response.status_code,
            reason=Reason.BAD_RESPONSE,
            error=f"HTTP {response.status_code}",
        )

    try:
        payload = response.json()
    except ValueError:
        return PoolProbe(
            ok=False,
            latency_ms=latency_ms,
            http_status=response.status_code,
            reason=Reason.BAD_RESPONSE,
            error="response was not JSON",
        )

    probe = parse_pool_stats(payload, adapter)
    if probe.ok and adapter.enrich is not None and adapter.extra_paths:
        bodies: list[Any] = []
        for extra in adapter.extra_paths:
            try:
                extra_response = await client.get(
                    pinned_url(resolved, extra),
                    headers={"host": resolved.hostname, "accept": "application/json"},
                    extensions={"sni_hostname": resolved.hostname},
                    timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
                )
                bodies.append(extra_response.json() if extra_response.status_code < 400 else None)
            except Exception:  # noqa: BLE001 - an extra is a bonus, never a failure
                bodies.append(None)
        try:
            probe = adapter.enrich(probe, bodies)
        except Exception:  # noqa: BLE001
            pass
    return probe.model_copy(
        update={"latency_ms": latency_ms, "http_status": response.status_code}
    )
