"""Server tests via fastapi TestClient (Stage 5; offline, no network).

A real ``Engine`` built on fake adapters (tmp_path cache, fake clock) is
injected into ``create_app`` — nothing here imports the real adapters, opens
the real data/ cache, or touches the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from prime_sportdata.cache import DiskCache
from prime_sportdata.engine import Engine
from prime_sportdata.errors import PrimeSportDataError, SourceBlocked, SourceUnavailable
from prime_sportdata.models import Event, ExternalRef, Team
from prime_sportdata.rate_limit import RateLimiter
from prime_sportdata.server.app import create_app
from prime_sportdata.sources.base import ParseOutcome, SourceAdapter, SourceResponse

FETCHED_AT = "2026-09-02T12:00:00Z"


class _FixedClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeAdapter(SourceAdapter):
    """Minimal programmable adapter for server-level scenarios."""

    source: ClassVar[str] = "fake"

    def __init__(
        self, name: str, error: PrimeSportDataError | None = None, *, empty: bool = False
    ) -> None:
        self._name = name
        self.error = error
        self.empty = empty
        self.fetch_calls = 0

    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        self.fetch_calls += 1
        if self.error is not None:
            raise self.error
        return SourceResponse(
            source=self._name,
            payload={"events": []},
            url=f"https://{self._name}.test/{sport}/{category}",
            status=200,
            fetched_at=FETCHED_AT,
        )

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        if self.empty:
            return ParseOutcome(events=[], warnings=[])
        events = [
            Event(
                sport="football",
                external=ExternalRef(source=self._name, source_event_id="m1"),
                start_time_utc="2026-09-02T19:00:00+00:00",
                status="scheduled",
                home=Team(name="Home FC"),
                away=Team(name="Away FC"),
                competition={"name": "Premier League", "country": "England"},
            )
        ]
        return ParseOutcome(events=events, warnings=["server-test warning"])


def make_engine(tmp_path: Path, adapters: Mapping[str, FakeAdapter]) -> Engine:
    clock = _FixedClock()
    return Engine(
        adapters=adapters,
        cache=DiskCache(tmp_path / "cache", clock=clock),
        limiter=RateLimiter(interval=1.0, clock=clock, sleep=lambda _s: None),
        clock=clock,
        now_iso=lambda: "2026-09-02T12:00:00+00:00",
    )


def healthy_adapters() -> dict[str, FakeAdapter]:
    return {
        name: FakeAdapter(name)
        for name in ("flashscore", "sofascore", "livescore", "betexplorer", "betika", "linebet")
    }


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    engine = make_engine(tmp_path, healthy_adapters())
    return TestClient(create_app(engine))


# --- /health ------------------------------------------------------------------


def test_health_ok_with_source_and_breaker_states(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # untested until a request reaches them; breaker states always present
    assert body["sources"] == {
        "flashscore": "untested",
        "sofascore": "untested",
        "livescore": "untested",
        "betexplorer": "untested",
        "betika": "untested",
        "linebet": "untested",
    }
    assert set(body["breakers"]) == {
        "flashscore",
        "sofascore",
        "livescore",
        "betexplorer",
        "betika",
        "linebet",
    }
    for view in body["breakers"].values():
        assert view["state"] == "closed"
        assert view["calls"] == 0


def test_health_reports_open_breaker_after_403(tmp_path: Path) -> None:
    adapters = healthy_adapters()
    adapters["flashscore"].error = SourceBlocked("HTTP 403 WAF", source="flashscore")
    engine = make_engine(tmp_path, adapters)
    with TestClient(create_app(engine)) as client:
        resp = client.get("/v1/football/fixtures?date=2026-09-02&limit=50")
        assert resp.status_code == 200  # failover to sofascore answered
        assert resp.json()["meta"]["source"] == "sofascore"
        health = client.get("/health").json()
        assert health["sources"]["flashscore"] == "blocked"
        assert health["breakers"]["flashscore"]["state"] == "open"
        assert health["breakers"]["flashscore"]["trips"] == 1


# --- /catalog -----------------------------------------------------------------


def test_catalog_lists_all_17_rows_with_params_and_curl(client: TestClient) -> None:
    resp = client.get("/catalog")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 17
    sports = {row["sport"] for row in rows}
    categories = {row["category"] for row in rows}
    assert sports == {"football", "basketball", "tennis"}
    assert categories == {
        "fixtures",
        "live",
        "results",
        "h2h",
        "odds",
        "odds_detailed",
        "odds_linebet",
    }
    for row in rows:
        assert row["params"] and row["sources"] and row["limit_default"] == 50
        assert row["limit_max"] == 200
        assert row["example_curl"].startswith("curl -s 'http://127.0.0.1:8097/v1/")
    live = next(r for r in rows if r["category"] == "live")
    assert "date" not in live["params"]
    h2h = next(r for r in rows if r["category"] == "h2h")
    assert set(h2h["params"]) == {"entity_a", "entity_b", "league", "limit"}


# --- /v1 envelope paths --------------------------------------------------------


def test_v1_fixtures_returns_200_envelope(client: TestClient) -> None:
    resp = client.get("/v1/football/fixtures?date=2026-09-02&league=Premier+League&limit=50")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["events"][0]["home"]["name"] == "Home FC"
    meta = body["meta"]
    assert meta["sport"] == "football"
    assert meta["category"] == "fixtures"
    assert meta["source"] == "flashscore"
    assert meta["cached"] is False
    assert meta["request"] == {"date": "2026-09-02", "league": "Premier League", "limit": 50}
    assert meta["warnings"] == ["server-test warning"]
    assert meta["latency_ms"] >= 0
    assert meta["fetched_at_utc"]


def test_v1_valid_empty_is_200_events_empty(tmp_path: Path) -> None:
    adapters = {
        name: FakeAdapter(name)
        for name in ("flashscore", "sofascore", "livescore", "betexplorer", "betika", "linebet")
    }
    adapters["flashscore"] = FakeAdapter("flashscore", empty=True)
    with TestClient(create_app(make_engine(tmp_path, adapters))) as client:
        resp = client.get("/v1/football/fixtures?date=2026-09-02")
    assert resp.status_code == 200
    assert resp.json()["data"]["events"] == []


def test_v1_h2h_requires_both_entities(client: TestClient) -> None:
    resp = client.get("/v1/football/h2h?entity_a=Arsenal")
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "bad_request"
    assert "entity_b" in err["detail"]
    ok = client.get("/v1/football/h2h?entity_a=Arsenal&entity_b=Chelsea")
    assert ok.status_code == 200  # engine-level h2h envelope with both names
    assert ok.json()["data"]["home"]["name"] == "Arsenal"


def test_v1_unknown_sport_is_404_error_body(client: TestClient) -> None:
    for path in ("/v1/cricket/fixtures", "/v1/football/standings"):
        resp = client.get(path)
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["code"] == "not_found"
        assert err["detail"]
        assert err["source"] is None
        assert err["tried_sources"] == []


def test_v1_bad_limit_is_400(client: TestClient) -> None:
    over = client.get("/v1/football/live?limit=500")
    assert over.status_code == 400
    assert over.json()["error"]["code"] == "bad_request"
    nan = client.get("/v1/football/live?limit=abc")
    assert nan.status_code == 400
    assert nan.json()["error"]["code"] == "bad_request"


def test_v1_bad_date_is_400(client: TestClient) -> None:
    resp = client.get("/v1/football/fixtures?date=02-09-2026")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"
    # live ignores date entirely (LIVE_PARAMS has no date key): no 400
    assert client.get("/v1/football/live?date=not-a-date").status_code == 200


def test_v1_all_sources_down_is_502_with_last_code_and_tried_sources(
    tmp_path: Path,
) -> None:
    adapters = healthy_adapters()
    adapters["flashscore"].error = SourceUnavailable("timeout", source="flashscore")
    adapters["sofascore"].error = SourceUnavailable("HTTP 503", source="sofascore")
    adapters["livescore"].error = SourceBlocked("HTTP 403", source="livescore")
    with TestClient(create_app(make_engine(tmp_path, adapters))) as client:
        resp = client.get("/v1/football/results?date=2026-09-01")
    assert resp.status_code == 502
    err = resp.json()["error"]
    assert err["code"] == "source_blocked"  # code of the LAST error
    assert err["source"] == "livescore"
    assert err["tried_sources"] == ["flashscore", "sofascore", "livescore"]
    assert "HTTP 403" in err["detail"]


def test_v1_404_shapes_and_errors_match_models(client: TestClient) -> None:
    from prime_sportdata.models import Envelope, ErrorBody

    ok = client.get("/v1/tennis/live?limit=10")
    Envelope.model_validate(ok.json())  # envelope matches models.py exactly
    bad = client.get("/v1/handball/live")
    ErrorBody.model_validate(bad.json())  # error body matches models.py exactly
    assert bad.status_code == 404
