"""Engine + circuit-breaker unit tests (Stage 5; offline, no network).

Fake adapters implement the base contract programmatically (succeed / raise
typed errors / return valid empties). The engine is exercised through
``fetch_on_demand`` with a tmp_path-backed cache, a fake clock pair and a
sleep-recording RateLimiter, so TTL, rate limiting and breaker cooldowns are
asserted without ever waiting or touching the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest

from prime_sportdata.cache import DiskCache
from prime_sportdata.engine import Engine, FetchFailed
from prime_sportdata.errors import (
    NoData,
    PrimeSportDataError,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.models import (
    Envelope,
    Event,
    EventsPayload,
    ExternalRef,
    H2HPayload,
    OddsPayload,
    OddsQuote,
    ScoreLine,
    Team,
)
from prime_sportdata.rate_limit import RateLimiter
from prime_sportdata.sources.base import ParseOutcome, SourceAdapter, SourceResponse

FETCHED_AT = "2026-09-02T12:00:00Z"


class FakeClock:
    """Injectable wall clock; tests advance it explicitly (never sleeps)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SleepRecorder:
    """Injectable RateLimiter sleep: records waits instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeAdapter(SourceAdapter):
    """Programmable adapter bound to one of the three catalog source names.

    The engine keys adapters by catalog name (the dict key), so the class-level
    ``source`` stays the base placeholder; the instance name is private
    (``_name``) and only used for response provenance. Configure per test:
    ``fetch_error`` / ``parse_error`` (typed errors — never bare exceptions,
    per the base contract), ``events`` and ``warnings`` for successful parses.
    """

    source: ClassVar[str] = "fake"

    def __init__(self, name: str) -> None:
        if name not in ("flashscore", "sofascore", "livescore", "betexplorer", "betika", "linebet"):
            raise ValueError(f"fake adapter must use a catalog source name, got {name!r}")
        self._name = name
        self.fetch_calls = 0
        self.parse_calls = 0
        self.fetch_error: PrimeSportDataError | None = None
        self.parse_error: PrimeSportDataError | None = None
        self.events: list[Event] = []
        self.quotes: list[OddsQuote] = []
        self.warnings: list[str] = []

    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        self.fetch_calls += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return SourceResponse(
            source=self._name,
            payload={"ok": True},
            url=f"https://{self._name}.test/{sport}/{category}",
            status=200,
            fetched_at=FETCHED_AT,
        )

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        self.parse_calls += 1
        if self.parse_error is not None:
            raise self.parse_error
        return ParseOutcome(events=list(self.events), warnings=list(self.warnings))

    def parse_odds(self, resp: SourceResponse) -> ParseOutcome:
        self.parse_calls += 1
        if self.parse_error is not None:
            raise self.parse_error
        return ParseOutcome(quotes=list(self.quotes), warnings=list(self.warnings))


def make_event(name: str = "Team A") -> Event:
    """One normalized event; caller-scoped by sport/id via model_copy in tests."""
    return Event(
        sport="football",
        external=ExternalRef(source="flashscore", source_event_id=f"e-{name}"),
        status="scheduled",
        home=Team(name=name),
        away=Team(name="Opponent"),
    )


def build_engine(
    tmp_path: Path,
    clock: FakeClock,
    sleeps: SleepRecorder,
    adapters: Mapping[str, FakeAdapter],
    **kwargs: Any,
) -> Engine:
    """Engine over a tmp_path cache + fake clock/sleep; never touches data/."""
    kwargs.setdefault("ttl_live_seconds", 30.0)
    kwargs.setdefault("ttl_default_seconds", 300.0)
    return Engine(
        adapters=adapters,
        cache=DiskCache(tmp_path / "cache", clock=clock),
        limiter=RateLimiter(interval=1.0, clock=clock, sleep=sleeps),
        clock=clock,
        now_iso=lambda: "2026-09-02T12:00:00+00:00",
        **kwargs,
    )


def make_adapters() -> dict[str, FakeAdapter]:
    return {
        name: FakeAdapter(name)
        for name in ("flashscore", "sofascore", "livescore", "betexplorer", "betika", "linebet")
    }


PARAMS: Mapping[str, Any] = {"date": "2026-09-02", "limit": 50}
REQUEST = ("football", "fixtures")


# --- failover basics ----------------------------------------------------------


def test_a_down_b_used(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].fetch_error = SourceUnavailable("TCP timeout", source="flashscore")
    adapters["sofascore"].events = [make_event("via sofascore")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    env = engine.fetch_on_demand(*REQUEST, PARAMS)
    assert env.meta.source == "sofascore"
    assert env.data.events[0].home.name == "via sofascore"
    assert adapters["flashscore"].fetch_calls == 1
    assert adapters["livescore"].fetch_calls == 0  # first healthy source wins


def test_a_up_b_untouched(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].events = [make_event("from flashscore")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    engine.fetch_on_demand(*REQUEST, PARAMS)
    assert adapters["sofascore"].fetch_calls == 0
    assert adapters["livescore"].fetch_calls == 0
    assert engine.fetch_on_demand(*REQUEST, PARAMS).meta.source == "flashscore"


def test_source_pinning_answers_only_that_source(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].events = [make_event("flashscore primary")]
    adapters["sofascore"].events = [make_event("sofascore pinned")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    env = engine.fetch_on_demand("football", "results", {"team": "Levante"}, source="sofascore")
    assert env.meta.source == "sofascore"
    assert env.data.events[0].home.name == "sofascore pinned"
    assert adapters["flashscore"].fetch_calls == 0
    assert adapters["livescore"].fetch_calls == 0
    # Pinned answers cache separately from the unpinned catalog order.
    engine.fetch_on_demand("football", "results", {"team": "Levante"})
    assert adapters["flashscore"].fetch_calls == 1


def test_source_pinning_unknown_source_raises_bad_request(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    engine = build_engine(tmp_path, clock, sleeps, make_adapters())
    from prime_sportdata.errors import BadRequest

    with pytest.raises(BadRequest):
        engine.fetch_on_demand(*REQUEST, PARAMS, source="nope")


def test_football_results_enrich_half_time_from_livescore(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    primary = make_event("Team A")
    secondary = make_event("Team A")
    secondary.status = "finished"
    secondary.score_lines = [ScoreLine(period_label="H1", home=1, away=0)]
    adapters["flashscore"].events = [primary]
    adapters["livescore"].events = [secondary]
    engine = build_engine(tmp_path, clock, sleeps, adapters)

    env = engine.fetch_on_demand("football", "results", {"date": "2026-09-02", "limit": 50})

    assert env.meta.source == "flashscore"
    assert len(env.data.events) == 1
    assert env.data.events[0].score_lines[0].period_label == "H1"
    assert any("enriched" in warning for warning in env.meta.warnings)
    # The merged envelope is cached: a second call answers without refetching.
    engine.fetch_on_demand("football", "results", {"date": "2026-09-02", "limit": 50})
    assert adapters["livescore"].fetch_calls == 1


def test_football_results_enrichment_failure_degrades_gracefully(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].events = [make_event("Team A")]
    adapters["livescore"].fetch_error = SourceBlocked("HTTP 403 WAF", source="livescore")
    engine = build_engine(tmp_path, clock, sleeps, adapters)

    env = engine.fetch_on_demand("football", "results", {"date": "2026-09-02", "limit": 50})

    assert env.meta.source == "flashscore"
    assert len(env.data.events) == 1
    assert any("enrichment skipped" in warning for warning in env.meta.warnings)


def test_all_down_raises_fetch_failed_with_last_code_and_tried(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].fetch_error = SourceUnavailable("503", source="flashscore")
    adapters["sofascore"].fetch_error = SourceUnavailable("timeout", source="sofascore")
    adapters["livescore"].fetch_error = RateLimited("HTTP 429", source="livescore")
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    with pytest.raises(FetchFailed) as excinfo:
        engine.fetch_on_demand(*REQUEST, PARAMS)
    exc = excinfo.value
    assert exc.last_code == "rate_limited"  # code of the LAST error
    assert exc.tried_sources == ["flashscore", "sofascore", "livescore"]
    assert exc.source == "livescore"
    assert exc.skipped_open == []
    assert "HTTP 429" in exc.detail


def test_valid_empty_from_healthy_source_is_200_empty_not_failover(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    # flashscore answers 200 with zero events: valid empty, NOT a failover trigger
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    env = engine.fetch_on_demand(*REQUEST, PARAMS)
    assert isinstance(env.data, EventsPayload)
    assert env.data.events == []
    assert env.meta.source == "flashscore"
    assert adapters["sofascore"].fetch_calls == 0


def test_events_truncated_to_limit_and_warnings_collected(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].events = [make_event(f"E{i}") for i in range(5)]
    adapters["flashscore"].warnings = ["standing: day feed needs caller-side filtering"]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    env = engine.fetch_on_demand(*REQUEST, {"date": "2026-09-02", "limit": 2})
    assert len(env.data.events) == 2
    assert env.meta.warnings == ["standing: day feed needs caller-side filtering"]
    assert env.meta.request.limit == 2


def test_fetch_nodata_is_resolution_empty_with_warning_not_failover(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    # sofascore-style: search answered 200 but found no such team -> NoData in
    # fetch. Engine must answer 200 empty + warning, never guess a team, and
    # never fetch another source for a query this source answered.
    adapters["sofascore"].fetch_error = NoData(
        "no football team found for 'Nonesuch FC' in search", source="sofascore"
    )
    order = {"football.fixtures": ["sofascore", "flashscore", "livescore"]}
    engine = build_engine(tmp_path, clock, sleeps, adapters, order_override=order)
    env = engine.fetch_on_demand(*REQUEST, {"date": "2026-09-02", "team": "Nonesuch FC", "limit": 50})
    assert env.meta.source == "sofascore"
    assert env.data.events == []
    assert any("Nonesuch FC" in w for w in env.meta.warnings)
    assert adapters["flashscore"].fetch_calls == 0
    assert isinstance(env, Envelope)


def test_parse_nodata_is_failover_stepping_stone(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    # healthy response whose content is unusable -> next source is tried
    adapters["flashscore"].parse_error = NoData("payload is not a day feed", source="flashscore")
    adapters["sofascore"].events = [make_event("backup answer")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    env = engine.fetch_on_demand(*REQUEST, PARAMS)
    assert env.meta.source == "sofascore"
    assert env.data.events[0].home.name == "backup answer"


# --- cache policy --------------------------------------------------------------


def test_cache_hit_returns_cached_envelope_without_fetch(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].events = [make_event("cached match")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    first = engine.fetch_on_demand(*REQUEST, PARAMS)
    assert first.meta.cached is False
    second = engine.fetch_on_demand(*REQUEST, PARAMS)
    assert second.meta.cached is True
    assert second.meta.source == "flashscore"
    assert adapters["flashscore"].fetch_calls == 1  # adapter never re-touched
    # TTL per category: fixtures/results/h2h share the long bucket
    clock.advance(299.0)
    assert engine.fetch_on_demand(*REQUEST, PARAMS).meta.cached is True
    clock.advance(2.0)
    assert engine.fetch_on_demand(*REQUEST, PARAMS).meta.cached is False


def test_live_ttl_is_short(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    engine.fetch_on_demand("football", "live", {"league": "x", "limit": 50})
    clock.advance(29.0)
    assert engine.fetch_on_demand("football", "live", {"league": "x", "limit": 50}).meta.cached
    clock.advance(2.0)
    assert not engine.fetch_on_demand("football", "live", {"league": "x", "limit": 50}).meta.cached


def test_cache_success_only(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].fetch_error = SourceUnavailable("down", source="flashscore")
    adapters["sofascore"].fetch_error = SourceUnavailable("down", source="sofascore")
    adapters["livescore"].fetch_error = SourceUnavailable("down", source="livescore")
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    for _ in range(2):
        with pytest.raises(FetchFailed):
            engine.fetch_on_demand(*REQUEST, PARAMS)
    assert adapters["flashscore"].fetch_calls == 2  # failed answers are never cached


def test_empty_odds_cached_only_briefly(tmp_path: Path) -> None:
    """Rollover/cold-start empties must not poison the odds cache.

    An empty 200 for an odds category is cached for the short
    ``ttl_empty_odds_seconds`` bucket (60s), never the full odds TTL (600s),
    so a caller retrying on the publish retry cadence reaches the live
    source instead of replaying emptiness for 10+ minutes.
    """
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    params = {"date": "2026-09-02", "limit": 50}
    first = engine.fetch_on_demand("football", "odds", params)
    assert first.data.quotes == []
    assert adapters["betexplorer"].fetch_calls == 1  # football/odds primary
    clock.advance(59.0)
    second = engine.fetch_on_demand("football", "odds", params)
    assert second.meta.cached is True
    assert adapters["betexplorer"].fetch_calls == 1
    clock.advance(2.0)
    third = engine.fetch_on_demand("football", "odds", params)
    assert third.meta.cached is False
    assert adapters["betexplorer"].fetch_calls == 2  # live scrape retried


def test_nonempty_odds_keep_full_ttl(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    quote = OddsQuote(
        sport="football",
        external=ExternalRef(source="betexplorer", source_event_id="evt-1"),
        start_time_utc=None,
        home="Team A",
        away="Team B",
        market="1x2",
        bookmaker="betexplorer-default",
        prices={"1": 1.8, "X": 3.4, "2": 4.2},
    )
    adapters["betexplorer"].quotes = [quote]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    params = {"date": "2026-09-02", "limit": 50}
    engine.fetch_on_demand("football", "odds", params)
    clock.advance(599.0)
    assert engine.fetch_on_demand("football", "odds", params).meta.cached is True
    clock.advance(2.0)
    assert engine.fetch_on_demand("football", "odds", params).meta.cached is False
    assert adapters["betexplorer"].fetch_calls == 2


# --- rate limiting -------------------------------------------------------------


def test_rate_limiter_serializes_two_same_host_calls_in_one_request(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].fetch_error = SourceUnavailable("down", source="flashscore")
    adapters["sofascore"].events = [make_event("after spacing")]
    engine = build_engine(
        tmp_path,
        clock,
        sleeps,
        adapters,
        hosts={"flashscore": "shared-host", "sofascore": "shared-host"},
    )
    engine.fetch_on_demand(*REQUEST, PARAMS)
    assert len(sleeps.calls) == 1  # second same-host call waited >= interval
    assert sleeps.calls[0] >= 1.0
    # and the limiter stamped both hosts' hits under one key


def test_different_hosts_never_block_each_other(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].fetch_error = SourceUnavailable("down", source="flashscore")
    adapters["sofascore"].events = [make_event("ok")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)  # DEFAULT_HOSTS differ
    engine.fetch_on_demand(*REQUEST, PARAMS)
    assert sleeps.calls == []


def test_rate_limit_applies_between_sequential_requests_to_one_source(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].events = [make_event("x")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-01", "limit": 50})
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-03", "limit": 50})
    assert sleeps.calls == [1.0]  # same host, second hit spaced by the interval


# --- circuit breaker -----------------------------------------------------------


def test_breaker_trips_on_403_then_source_skipped_while_b_answers(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].fetch_error = SourceBlocked("HTTP 403 WAF", source="flashscore")
    adapters["sofascore"].events = [make_event("b answers")]
    engine = build_engine(tmp_path, clock, sleeps, adapters, breaker_cooldown_seconds=1800.0)
    # the 403 trips the breaker; the same request continues to sofascore
    env = engine.fetch_on_demand(*REQUEST, PARAMS)
    assert env.meta.source == "sofascore"
    view = engine.breaker_views()["flashscore"]
    assert view.state == "open"
    assert view.status == "blocked"
    assert view.trips == 1
    # during the cooldown flashscore is SKIPPED (adapter not called), B answers
    calls_before = adapters["flashscore"].fetch_calls
    adapters["flashscore"].fetch_error = None  # even if it healed, breaker holds
    adapters["flashscore"].events = [make_event("healed")]
    env2 = engine.fetch_on_demand(*REQUEST, {"date": "2026-09-03", "limit": 50})
    assert env2.meta.source == "sofascore"
    assert adapters["flashscore"].fetch_calls == calls_before  # never called
    assert engine.breaker_views()["flashscore"].state == "open"


def test_breaker_does_not_trip_on_healthy_responses_or_isolated_failures(
    tmp_path: Path,
) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].events = [make_event("ok")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    # one 5xx-class failure, then healthy, then another single failure:
    # consecutive counter resets on the healthy response -> never opens
    adapters["flashscore"].fetch_error = SourceUnavailable("503", source="flashscore")
    adapters["sofascore"].events = [make_event("b")]
    engine.fetch_on_demand(*REQUEST, PARAMS)
    view = engine.breaker_views()["flashscore"]
    assert view.state == "closed"
    assert view.consecutive_failures == 1
    adapters["flashscore"].fetch_error = None
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-03", "limit": 50})
    adapters["flashscore"].fetch_error = SourceUnavailable("504", source="flashscore")
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-04", "limit": 50})
    view = engine.breaker_views()["flashscore"]
    assert view.state == "closed"
    assert view.consecutive_failures == 1  # reset happened in between
    assert view.trips == 0


def test_two_consecutive_5xx_trip_breaker_and_status_is_unavailable(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].fetch_error = SourceUnavailable("timeout", source="flashscore")
    adapters["sofascore"].events = [make_event("b")]
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-01", "limit": 50})
    assert engine.breaker_views()["flashscore"].state == "closed"
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-02", "limit": 50})
    view = engine.breaker_views()["flashscore"]
    assert view.state == "open"
    assert view.status == "unavailable"
    assert view.consecutive_failures == 0  # consumed by the trip
    assert view.trips == 1


def test_breaker_escalation_and_half_open_recovery(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    for name in adapters:
        adapters[name].fetch_error = SourceUnavailable("down", source=name)
    engine = build_engine(
        tmp_path,
        clock,
        sleeps,
        adapters,
        breaker_cooldown_seconds=10.0,
        breaker_cooldown_max_seconds=40.0,
    )
    # two consecutive 5xx-class failures per source -> every breaker opens (trip 1)
    with pytest.raises(FetchFailed):
        engine.fetch_on_demand(*REQUEST, {"date": "2026-09-01", "limit": 50})
    with pytest.raises(FetchFailed):
        engine.fetch_on_demand(*REQUEST, {"date": "2026-09-02", "limit": 50})
    fs_view = engine.breaker_views()["flashscore"]
    assert fs_view.state == "open" and fs_view.trips == 1 and fs_view.cooldown_seconds == 10.0
    clock.advance(11.0)  # cooldown over -> next request is a half-open probe
    adapters["flashscore"].fetch_error = RateLimited("HTTP 429", source="flashscore")
    with pytest.raises(FetchFailed):
        engine.fetch_on_demand(*REQUEST, {"date": "2026-09-03", "limit": 50})
    fs_view = engine.breaker_views()["flashscore"]
    # probe failed -> open again, ESCALATED (doubled) and never retried into
    assert fs_view.state == "open"
    assert fs_view.status == "blocked"  # the probe failed with a 429
    assert fs_view.trips == 2
    assert fs_view.cooldown_seconds == 20.0
    clock.advance(21.0)  # escalated cooldown over
    adapters["flashscore"].fetch_error = None
    adapters["flashscore"].events = [make_event("recovered")]
    env = engine.fetch_on_demand(*REQUEST, {"date": "2026-09-04", "limit": 50})
    assert env.meta.source == "flashscore"  # probe succeeded
    fs_view = engine.breaker_views()["flashscore"]
    assert fs_view.state == "closed"
    assert fs_view.trips == 0  # recovery resets the escalation level
    assert fs_view.status == "ok"


def test_half_open_allows_exactly_one_probe_request(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    adapters["flashscore"].fetch_error = SourceBlocked("403", source="flashscore")
    adapters["sofascore"].events = [make_event("b")]
    engine = build_engine(tmp_path, clock, sleeps, adapters, breaker_cooldown_seconds=10.0)
    # request 1 (t=0): flashscore 403 -> trips (open until t=10); sofascore answers
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-01", "limit": 50})
    assert adapters["flashscore"].fetch_calls == 1
    # request 2 (t=0, still in cooldown): flashscore skipped
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-02", "limit": 50})
    assert adapters["flashscore"].fetch_calls == 1
    clock.advance(11.0)  # cooldown elapsed
    # request 3: exactly ONE half-open probe is admitted; it fails -> reopen.
    # (sofascore + livescore must fail too, or failover would answer.)
    adapters["sofascore"].fetch_error = SourceUnavailable("down", source="sofascore")
    adapters["livescore"].fetch_error = SourceUnavailable("down", source="livescore")
    with pytest.raises(FetchFailed):
        engine.fetch_on_demand(*REQUEST, {"date": "2026-09-03", "limit": 50})
    assert adapters["flashscore"].fetch_calls == 2  # probe only, not more
    assert engine.breaker_views()["flashscore"].state == "open"
    # request 4 (t=11, inside the escalated 20 s cooldown): skipped again
    adapters["sofascore"].fetch_error = None
    adapters["livescore"].fetch_error = None
    engine.fetch_on_demand(*REQUEST, {"date": "2026-09-04", "limit": 50})
    assert adapters["flashscore"].fetch_calls == 2


def test_all_sources_breaker_skipped_terminal_error_mentions_state(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    for name in adapters:
        adapters[name].fetch_error = SourceUnavailable("down", source=name)
    engine = build_engine(
        tmp_path,
        clock,
        sleeps,
        adapters,
        breaker_cooldown_seconds=10.0,
        breaker_cooldown_max_seconds=40.0,
    )
    for _ in range(4):  # every source trips -> open during the cooldown
        with pytest.raises(FetchFailed):
            engine.fetch_on_demand(*REQUEST, {"date": "2026-09-01", "limit": 50})
        clock.advance(20.0)
    with pytest.raises(FetchFailed) as excinfo:
        engine.fetch_on_demand(*REQUEST, {"date": "2026-09-02", "limit": 50})
    exc = excinfo.value
    assert exc.skipped_open == ["flashscore", "sofascore", "livescore"]
    assert "open circuit breaker" in exc.detail
    assert exc.last_code == "source_unavailable"


def test_h2h_envelope_payload_carries_requested_entities(tmp_path: Path) -> None:
    clock, sleeps = FakeClock(), SleepRecorder()
    adapters = make_adapters()
    order = {"football.h2h": ["flashscore", "sofascore", "livescore"]}
    engine = build_engine(tmp_path, clock, sleeps, adapters, order_override=order)
    params = {"entity_a": "Arsenal", "entity_b": "Chelsea", "league": "Premier League"}
    adapters["flashscore"].events = [make_event("meeting")]
    env = engine.fetch_on_demand("football", "h2h", params)
    assert isinstance(env.data, H2HPayload)
    assert env.data.home.name == "Arsenal"
    assert env.data.away.name == "Chelsea"
    assert env.meta.category == "h2h"
    # h2h shares the long TTL bucket; league echoed back
    assert env.meta.request.league == "Premier League"


def test_odds_linebet_category_dispatches_to_parse_odds(tmp_path: Path) -> None:
    """Regression: the linebet-only odds row must hit parse_odds, never events.

    The catalog row was shipped before the engine dispatch knew its name, so
    hosted calls fell into parse_events and failed with a misleading no_data.
    """

    sample_quote = OddsQuote(
        sport="football",
        external=ExternalRef(source="linebet", source_event_id="evt-1"),
        start_time_utc=None,
        home="Team A",
        away="Team B",
        market="btts",
        bookmaker="linebet",
        prices={"yes": 1.8, "no": 2.0},
    )

    class OddsFake(FakeAdapter):
        def __init__(self) -> None:
            super().__init__("linebet")
            self.odds_parse_calls = 0

        def parse_odds(self, resp: SourceResponse) -> ParseOutcome:
            self.odds_parse_calls += 1
            return ParseOutcome(quotes=[sample_quote])

    linebet = OddsFake()
    adapters = make_adapters()
    adapters["linebet"] = linebet
    clock, sleeps = FakeClock(), SleepRecorder()
    engine = build_engine(tmp_path, clock, sleeps, adapters)
    envelope = engine.fetch_on_demand("football", "odds_linebet", {"limit": "3"})
    assert linebet.odds_parse_calls == 1
    assert envelope.meta.category == "odds_linebet"
    # Regression: parsed odds_linebet quotes must be shipped as an odds
    # payload.  The response builder previously fell through to the events
    # branch and silently dropped every linebet quote.
    assert isinstance(envelope.data, OddsPayload)
    assert envelope.data.quotes == [sample_quote]
