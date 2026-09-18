"""Fetch engine: cache -> rate limit -> per-source circuit breakers -> failover.

Stage 5 artifact. Implements SPEC "Architecture" (``fetch_on_demand(sport,
category, params) -> Envelope``), "Ban-risk policy" §2 (per-source circuit
breaker), §4 (cache-first, cache success only, category TTLs) and §5 (never
retry into a block), and "Errors" (valid empty = 200 ``events: []``; total
failure = terminal error carrying the last error's code + ``tried_sources``,
mapped to 502 by the routes; blocked/rate-limited/5xx per source are stepping
stones to that terminal error, not terminal themselves).

Request pipeline
----------------
1. **Cache lookup**: sha1 key over canonical JSON of sport + category +
   normalized (string-valued) params. TTL per category: ``live`` ->
   ``ttl_live_seconds``; fixtures/results/h2h -> ``ttl_default_seconds`` (the
   fixtures bucket; results/h2h reuse it — config comment precedent). Only
   fresh entries short-circuit (``meta.cached=true``, adapter never touched);
   stale entries are refetched and overwritten on success (no stale-if-error).
   Only successful answers are cached — an all-fail request is never cached.
2. **Rate limiter**: ``RateLimiter.acquire`` before EVERY adapter call, keyed
   by the adapter's host when known (``DEFAULT_HOSTS``, overridable) else the
   source name; never faster than the configured interval.
3. **Failover**: ordered per ``catalog.source_order`` (override applied).
   First source that answers wins. Adapters raising ``NoData`` *from fetch* are
   treated as resolution-style empties (team/entity/date resolution failed):
   that source wins with a 200 empty envelope + the error detail as a warning
   — never a wrong-team guess (SPEC "API surface" advisory-filter rule) and
   never an extra fetch into another source. ``NoData`` *from parse_events*
   (healthy response, unusable content) is a stepping stone like
   blocked/rate-limited/unavailable/not-found/bad-request: failover continues,
   and if nothing else answers the last error becomes terminal.
4. **Envelope**: ``meta.source`` = answering source; ``meta.warnings`` =
   collected ParseOutcome warnings; ``fetched_at_utc``/``latency_ms`` from the
   injected clock pair; ``events`` truncated to ``limit`` when given (adapter
   results may exceed it; the envelope never does). h2h responses carry
   ``H2HPayload`` with the requested entity names (``summary`` stays None —
   no adapter derives one this build; SPEC: omit when not derivable).

Circuit breaker (per source, in-memory; no DB/scheduler)
---------------------------------------------------------
States ``closed -> open -> half_open``:

* trip to ``open`` on one ``SourceBlocked`` or ``RateLimited`` from that
  source, or on ``breaker_trip_after`` (default 2) *consecutive*
  ``SourceUnavailable`` (5xx/timeout-class). Other typed errors (NotFound,
  NoData, BadRequest) prove the transport answered and reset the consecutive
  counter instead of tripping. Healthy responses never trip.
* while ``open`` the source is skipped in failover order (its adapter is NOT
  called) and its state is surfaced via ``breaker_views()`` for /health.
* after the cooldown elapses, the next request that reaches the source is a
  **half-open probe** (exactly one request; further requests are denied until
  it resolves). Probe success -> ``closed`` with a fresh escalation level;
  probe failure (ANY typed error) -> ``open`` again, escalated.
* cooldown escalates by doubling per trip from ``breaker_cooldown_seconds``
  (default 1800 s = 30 min) toward ``breaker_cooldown_max_seconds`` (default
  14400 s = 4 h), e.g. 30 min -> 1 h -> 2 h -> 4 h (capped). Recovery resets
  the level. Clock is injectable (unit tests advance it without sleeping).

Never retry into a block: a 403/429 raises before any retry inside the
adapter; at engine level it trips that source's breaker and failover moves to
the next source in the same request.

Terminal outcome: every ordered source failed -> ``FetchFailed`` raised with
``last_code``/``detail``/``source`` mirroring the last recorded error and
``tried_sources`` = the ordered sources for this cell (SPEC error body shape);
when sources were skipped by open breakers the detail says so and
``skipped_open`` lists them. The routes map ``FetchFailed`` to HTTP 502.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

from prime_sportdata.cache import DiskCache
from prime_sportdata.catalog import SOURCES, get_row, source_order
from prime_sportdata.errors import (
    BadRequest,
    NoData,
    NotFound,
    PrimeSportDataError,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.models import (
    Category,
    Envelope,
    EnvelopeData,
    Event,
    EventsPayload,
    H2HEntity,
    H2HPayload,
    Meta,
    OddsPayload,
    OddsQuote,
    RequestParams,
    Sport,
)
from prime_sportdata.rate_limit import RateLimiter
from prime_sportdata.sources.base import ParseOutcome, SourceAdapter, SourceResponse

# Adapter hosts used as rate-limiter keys (SPEC politeness is per-host).
DEFAULT_HOSTS: Mapping[str, str] = {
    "flashscore": "2.flashscore.ninja",
    "sofascore": "www.sofascore.com",
    "livescore": "www.livescore.com",
    "betexplorer": "www.betexplorer.com",
    "betika": "api.betika.com",
    "linebet": "linebet.com",
}

_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class BreakerView:
    """JSON-friendly snapshot of one source's breaker for /health."""

    status: str  # ok | unavailable | blocked | untested (SPEC /health values)
    state: str  # closed | open | half_open
    calls: int
    consecutive_failures: int
    trips: int
    cooldown_seconds: float
    open_until_utc: str | None


class CircuitBreaker:
    """Per-source ban-risk gate (SPEC Ban-risk policy §2). Injectable clock.

    State machine (see module docstring for the full policy): ``closed``
    allows every request and trips on the documented signals; ``open`` denies
    requests until the (escalating) cooldown passes, then admits exactly one
    half-open probe; the probe's outcome decides close vs. reopen-escalated.

    All transitions are serialized with a lock so concurrent requests cannot
    double-admit half-open probes.
    """

    def __init__(
        self,
        source: str,
        *,
        cooldown_seconds: float = 1800.0,
        cooldown_max_seconds: float = 14400.0,
        trip_after_consecutive: int = 2,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.source = source
        self._cooldown_base = cooldown_seconds
        self._cooldown_max = cooldown_max_seconds
        self._trip_after = max(1, trip_after_consecutive)
        self._clock = clock
        self._lock = threading.Lock()
        self._state = _CLOSED
        self._kind: str | None = None  # "blocked" (403/429) | "unavailable" (5xx)
        self._probe_in_flight = False
        self._calls = 0
        self._consecutive = 0
        self._trips = 0
        self._cooldown = cooldown_seconds
        self._opened_at: float | None = None

    # -- request gate --------------------------------------------------------

    def allow(self) -> bool:
        """Decide whether the engine may call this source's adapter now.

        ``closed`` -> allow. ``open`` past its cooldown -> transition to
        ``half_open`` and allow exactly this one probe. ``open`` (still in
        cooldown) or ``half_open`` (probe already admitted) -> deny.
        """
        with self._lock:
            if self._state == _OPEN:
                opened = self._opened_at
                if opened is None or self._clock() < opened + self._cooldown:
                    return False
                self._state = _HALF_OPEN  # this request is the single probe
            if self._state == _HALF_OPEN:
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
            self._calls += 1
            return True

    # -- outcomes ------------------------------------------------------------

    def record_success(self) -> None:
        """Healthy outcome (usable answer, including valid empty)."""
        with self._lock:
            self._probe_in_flight = False
            self._consecutive = 0
            if self._state == _HALF_OPEN:
                # Probe passed: recovery resets the escalation level.
                self._state = _CLOSED
                self._kind = None
                self._trips = 0
                self._opened_at = None
                self._opened_at_iso = None

    def record_failure(self, exc: PrimeSportDataError) -> None:
        """Typed failure from this source's adapter (fetch or parse)."""
        with self._lock:
            self._probe_in_flight = False
            if isinstance(exc, (SourceBlocked, RateLimited)):
                # A 403/429/challenge is a ban signal: open immediately.
                self._open("blocked")
                return
            if isinstance(exc, SourceUnavailable):
                if self._state == _HALF_OPEN:
                    self._open("unavailable")  # probe failure -> escalated reopen
                    return
                self._consecutive += 1
                if self._consecutive >= self._trip_after:
                    self._open("unavailable")
                return
            # Any other typed error proved the transport answered (NotFound /
            # NoData / BadRequest): not a ban or outage signal.
            if self._state == _HALF_OPEN:
                self._open("unavailable")  # probe failed -> open again (escalated)
                return
            self._consecutive = 0

    def _open(self, kind: str) -> None:
        self._trips += 1
        # Escalation: 30 min, doubling per trip, capped at 4 h (documented).
        self._cooldown = min(self._cooldown_base * (2 ** (self._trips - 1)), self._cooldown_max)
        self._state = _OPEN
        self._kind = kind
        self._consecutive = 0
        self._opened_at = self._clock()

    # -- views ---------------------------------------------------------------

    def view(self) -> BreakerView:
        """Snapshot for /health: state + SPEC source status + counters."""
        with self._lock:
            state = self._state
            kind = self._kind
            if state == _CLOSED:
                status = "ok" if self._calls else "untested"
            else:
                status = "blocked" if kind == "blocked" else "unavailable"
            opened_at = self._opened_at
            open_until_utc: str | None = None
            if state == _OPEN and opened_at is not None:
                until = datetime.fromtimestamp(opened_at + self._cooldown, tz=UTC).isoformat()
                open_until_utc = until
            elif state == _HALF_OPEN and opened_at is not None:
                open_until_utc = datetime.fromtimestamp(opened_at + self._cooldown, tz=UTC).isoformat()
            return BreakerView(
                status=status,
                state=state,
                calls=self._calls,
                consecutive_failures=self._consecutive,
                trips=self._trips,
                cooldown_seconds=self._cooldown,
                open_until_utc=open_until_utc,
            )


class FetchFailed(PrimeSportDataError):
    """Terminal failover failure: every ordered source failed (SPEC "Errors").

    ``last_code``/``detail``/``source`` mirror the *last* recorded error so the
    routes can emit the SPEC 502 body with a single terminal error code;
    ``tried_sources`` lists the ordered sources for the request; ``skipped_open``
    lists sources the breakers denied (mentioned in ``detail`` when relevant).
    """

    code: ClassVar[str] = "fetch_failed"

    def __init__(
        self,
        last: PrimeSportDataError | None,
        tried_sources: Sequence[str],
        skipped_open: Sequence[str] = (),
    ) -> None:
        detail = ""
        if last is not None:
            detail = last.detail
            source = last.source
        else:
            source = None
        if skipped_open:
            note = "sources skipped by open circuit breakers: " + ", ".join(skipped_open)
            detail = f"{note}; last error: {detail}" if detail else note
        if not detail:
            detail = "every ordered source failed without a recorded typed error"
        super().__init__(detail, source=source)
        self.last_code: str = last.code if last is not None else "source_unavailable"
        self.tried_sources: list[str] = list(tried_sources)
        self.skipped_open: list[str] = list(skipped_open)


class Engine:
    """The Stage 5 fetch engine. Dependency-injected; no network at import.

    Tests inject fake adapters (named after real sources — catalog failover
    orders only contain source names), a ``DiskCache`` over ``tmp_path``, a
    ``RateLimiter`` with a fake clock/sleep, and an injectable clock pair.
    """

    def __init__(
        self,
        *,
        adapters: Mapping[str, SourceAdapter],
        cache: DiskCache,
        limiter: RateLimiter,
        order_override: Mapping[str, Sequence[str]] | None = None,
        hosts: Mapping[str, str] | None = None,
        ttl_live_seconds: float = 30.0,
        ttl_default_seconds: float = 1800.0,
        ttl_odds_seconds: float = 600.0,
        breaker_enabled: bool = True,
        breaker_cooldown_seconds: float = 1800.0,
        breaker_cooldown_max_seconds: float = 14400.0,
        breaker_trip_after: int = 2,
        clock: Callable[[], float] = time.time,
        now_iso: Callable[[], str] = _utc_now_iso,
    ) -> None:
        missing = [name for name in SOURCES if name not in adapters]
        if missing:
            raise ValueError(f"engine requires every catalog source adapter; missing: {missing}")
        self._adapters = dict(adapters)
        self._cache = cache
        self._limiter = limiter
        self._order_override = dict(order_override) if order_override else None
        self._hosts = {**DEFAULT_HOSTS, **(hosts or {})}
        self._ttl_live = ttl_live_seconds
        self._ttl_default = ttl_default_seconds
        self._ttl_odds = ttl_odds_seconds
        self._breaker_enabled = breaker_enabled
        self._clock = clock
        self._now_iso = now_iso
        self._breakers = {
            name: CircuitBreaker(
                name,
                cooldown_seconds=breaker_cooldown_seconds,
                cooldown_max_seconds=breaker_cooldown_max_seconds,
                trip_after_consecutive=breaker_trip_after,
                clock=clock,
            )
            for name in self._adapters
        }

    # -- public API ----------------------------------------------------------

    def fetch_on_demand(
        self,
        sport: str,
        category: str,
        params: Mapping[str, Any],
        *,
        source: str | None = None,
    ) -> Envelope:
        """Cache -> rate limit -> breakers -> ordered failover -> Envelope.

        ``source`` pins the request to one source (validated against the
        catalog); None keeps the catalog failover order.
        """
        sport, category = str(sport), str(category)
        try:
            get_row(cast(Sport, sport), cast(Category, category))
        except KeyError:
            raise NotFound(f"unknown sport/category: {sport}/{category}") from None
        if source is not None:
            source = source.strip()
            if source not in SOURCES:
                raise BadRequest(
                    f"unknown source {source!r} (known sources: {', '.join(SOURCES)})"
                )
            order: Sequence[str] = (source,)
        else:
            order = source_order(cast(Sport, sport), cast(Category, category), self._order_override)
        norm = self._normalize_params(params)
        key = self._cache_key(sport, category, norm, source)
        payload, fresh, _stored = self._cache.get(key)
        if fresh and isinstance(payload, dict):
            return self._envelope_from_cache(sport, category, payload)
        envelope = self._fetch_uncached(sport, category, norm, order)
        if category == "live":
            ttl = self._ttl_live
        elif category in ("odds", "odds_detailed"):
            ttl = self._ttl_odds
        else:
            ttl = self._ttl_default
        self._cache.set(key, envelope.model_dump(mode="json"), ttl)
        return envelope

    def breaker_views(self) -> dict[str, BreakerView]:
        """Per-source breaker snapshots for /health."""
        return {name: breaker.view() for name, breaker in self._breakers.items()}

    # -- internals -----------------------------------------------------------

    def _fetch_uncached(
        self, sport: str, category: str, norm: Mapping[str, str], order: Sequence[str]
    ) -> Envelope:
        last_error: PrimeSportDataError | None = None
        skipped_open: list[str] = []
        for source in order:
            breaker = self._breakers[source]
            if self._breaker_enabled and not breaker.allow():
                skipped_open.append(source)
                continue
            host = self._hosts.get(source, source)
            self._limiter.acquire(host)
            adapter = self._adapters[source]
            started = self._clock()
            try:
                try:
                    resp: SourceResponse = adapter.fetch(sport, category, norm)
                except NoData as exc:
                    # Resolution-style empty: source answered, nothing matched
                    # this query (e.g. sofascore search found no such team).
                    # 200 empty + warning; NOT a failover trigger (SPEC rule).
                    breaker.record_success()
                    return self._envelope_for(
                        sport, category, source, norm, [], warnings=[exc.detail], started=started
                    )
                try:
                    if category in ("odds", "odds_detailed", "odds_linebet"):
                        outcome: ParseOutcome = adapter.parse_odds(resp)
                    else:
                        outcome = adapter.parse_events(resp)
                except PrimeSportDataError as exc:
                    breaker.record_failure(exc)
                    last_error = exc
                    continue
            except PrimeSportDataError as exc:
                breaker.record_failure(exc)
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001 — adapter boundary: an untyped
                # bug must never kill the request; it is mapped to a typed
                # SourceUnavailable stepping stone and surfaced honestly.
                wrapped = SourceUnavailable(
                    f"adapter {source} raised an untyped exception: {exc!r}",
                    source=source,
                )
                breaker.record_failure(wrapped)
                last_error = wrapped
                continue
            breaker.record_success()
            events = list(outcome.events)
            warnings = list(outcome.warnings)
            if (
                sport == "football"
                and category == "results"
                and norm.get("date")
                and source == "flashscore"
                and events
            ):
                # flashscore day tables ship final scores only.  Livescore's
                # date API carries half-time lines and postponed statuses, so
                # a best-effort enrichment merge runs on every cache miss;
                # failures degrade to the un-enriched primary list.
                events, merge_warnings = self._enrich_football_results(norm, events)
                warnings.extend(merge_warnings)
            events = self._truncate(events, norm)
            quotes = self._truncate_quotes(outcome.quotes, norm)
            return self._envelope_for(
                sport,
                category,
                source,
                norm,
                events,
                quotes,
                warnings=warnings,
                started=started,
            )
        raise FetchFailed(last_error, list(order), skipped_open)

    def _enrich_football_results(
        self,
        norm: Mapping[str, str],
        events: list[Event],
    ) -> tuple[list[Event], list[str]]:
        """Best-effort livescore enrichment for football day-table results."""
        from prime_sportdata.sources.merge import merge_football_results

        host = self._hosts.get("livescore", "livescore")
        self._limiter.acquire(host)
        adapter = self._adapters["livescore"]
        try:
            response = adapter.fetch("football", "results", {"date": norm["date"]})
        except PrimeSportDataError as exc:
            return events, [f"football results half-time enrichment skipped: {exc.detail}"]
        except Exception as exc:  # noqa: BLE001 — enrichment must never kill the request
            return events, [f"football results half-time enrichment skipped (untyped): {exc!r}"]
        try:
            outcome = adapter.parse_events(response)
        except PrimeSportDataError as exc:
            return events, [f"football results half-time enrichment skipped: {exc.detail}"]
        except Exception as exc:  # noqa: BLE001 — enrichment must never kill the request
            return events, [f"football results half-time enrichment skipped (untyped parse): {exc!r}"]
        relevant = [
            event
            for event in outcome.events
            if event.status in {"finished", "postponed", "cancelled"}
        ]
        return merge_football_results(events, relevant)

    def _envelope_for(
        self,
        sport: str,
        category: str,
        source: str,
        norm: Mapping[str, str],
        events: Sequence[Event],
        quotes: Sequence[OddsQuote] = (),
        *,
        warnings: Sequence[str],
        started: float,
    ) -> Envelope:
        latency_ms = max(0, int((self._clock() - started) * 1000))
        data = self._data_payload(category, norm, events, quotes)
        limit: int | None = None
        raw_limit = norm.get("limit")
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = None
        meta = Meta(
            sport=cast(Sport, sport),
            category=cast(Category, category),
            source=source,
            cached=False,
            fetched_at_utc=self._now_iso(),
            latency_ms=latency_ms,
            request=RequestParams(date=norm.get("date"), league=norm.get("league"), limit=limit),
            warnings=list(warnings),
        )
        return Envelope(data=data, meta=meta)

    def _data_payload(
        self,
        category: str,
        norm: Mapping[str, str],
        events: Sequence[Event],
        quotes: Sequence[OddsQuote] = (),
    ) -> EnvelopeData:
        if category == "h2h":
            return H2HPayload(
                events=list(events),
                home=H2HEntity(name=norm.get("entity_a") or ""),
                away=H2HEntity(name=norm.get("entity_b") or ""),
            )
        if category in ("odds", "odds_detailed", "odds_linebet"):
            return OddsPayload(quotes=list(quotes))
        return EventsPayload(events=list(events))

    def _envelope_from_cache(self, sport: str, category: str, payload: dict[str, Any]) -> Envelope:
        envelope = Envelope.model_validate(payload)
        meta = envelope.meta.model_copy(update={"cached": True, "latency_ms": 0})
        return envelope.model_copy(update={"meta": meta})

    @staticmethod
    def _normalize_params(params: Mapping[str, Any]) -> dict[str, str]:
        return {str(k): str(v) for k, v in params.items() if v is not None}

    @staticmethod
    def _truncate(events: Sequence[Event], norm: Mapping[str, str]) -> list[Event]:
        raw = norm.get("limit")
        if raw is None:
            return list(events)
        try:
            limit = int(raw)
        except ValueError:
            return list(events)
        if limit < 0:
            return list(events)
        return list(events[:limit])

    @staticmethod
    def _truncate_quotes(quotes: Sequence[OddsQuote], norm: Mapping[str, str]) -> list[OddsQuote]:
        raw = norm.get("limit")
        if raw is None:
            return list(quotes)
        try:
            limit = int(raw)
        except ValueError:
            return list(quotes)
        if limit < 0:
            return list(quotes)
        return list(quotes[:limit])

    @staticmethod
    def _cache_key(sport: str, category: str, norm: Mapping[str, str], source: str | None = None) -> str:
        canonical = json.dumps(
            {
                "sport": sport,
                "category": category,
                "source": source,
                "params": sorted(norm.items()),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_HOSTS",
    "BreakerView",
    "CircuitBreaker",
    "Engine",
    "FetchFailed",
    "_utc_now_iso",
]
