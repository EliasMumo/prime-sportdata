"""Offline Linebet adapter tests against live-captured fixtures (2026-09-09).

The JSON fixtures under ``tests/fixtures/`` are trimmed rows from real HTTP
200 responses captured on 2026-09-09 from linebet.com's public JSON API
(see the adapter module docstring for the exact verified URLs).  No network
is touched here: fetch tests monkeypatch the adapter's transport with the
same fixture bodies.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from prime_sportdata.errors import BadRequest, NotFound
from prime_sportdata.sources.base import SourceResponse
from prime_sportdata.sources.linebet import LinebetAdapter

FIXTURES = Path(__file__).parent / "fixtures"

_BARCELONA_CI = "364649426"


def _list_body() -> dict:
    return json.loads((FIXTURES / "linebet_matches.json").read_text(encoding="utf-8"))


def _gamezip_body() -> dict:
    return json.loads((FIXTURES / "linebet_gamezip.json").read_text(encoding="utf-8"))


def _h2h_body() -> dict:
    return json.loads((FIXTURES / "linebet_h2h.json").read_text(encoding="utf-8"))


@pytest.fixture()
def adapter() -> LinebetAdapter:
    return LinebetAdapter(clock=lambda: 0.0, sleep=lambda _: None)


def _assembled_odds_response() -> SourceResponse:
    payload = {
        "kind": "odds",
        "payload": _list_body(),
        "details": {_BARCELONA_CI: _gamezip_body()["Value"]},
        "detail_skipped": [],
    }
    return SourceResponse(
        source="linebet",
        payload=json.dumps(payload),
        url="https://linebet.com/service-api/LineFeed/Get1x2_VZip",
        status=200,
        fetched_at="2026-09-09T10:00:00+00:00",
    )


def _assembled_h2h_response() -> SourceResponse:
    match = next(
        event for event in _list_body()["Value"] if event.get("O1") == "Liverpool"
    )
    payload = {"kind": "h2h", "match": match, "swapped": False, "payload": _h2h_body()}
    return SourceResponse(
        source="linebet",
        payload=json.dumps(payload),
        url="https://linebet.com/service-api/statisticfeed/api/v1/Game/h2h",
        status=200,
        fetched_at="2026-09-09T10:00:00+00:00",
    )


# -- parse_odds -------------------------------------------------------------


def test_parse_odds_emits_verified_markets_only(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_odds_response())
    markets = {quote.market for quote in outcome.quotes}
    assert "1x2" in markets
    assert "double_chance" in markets
    assert "btts" in markets
    assert "correct_score" in markets
    assert any(market.startswith("total_") for market in markets)
    # HT/FT is deliberately not shipped (ambiguous encoding, see docstring).
    assert "htft" not in markets


def test_parse_odds_1x2_matches_live_capture(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_odds_response())
    barcelona = next(
        quote
        for quote in outcome.quotes
        if quote.market == "1x2" and quote.home == "Barcelona"
    )
    assert barcelona.prices == {"1": 1.065, "X": 13.0, "2": 25.0}
    assert barcelona.bookmaker == "linebet"
    assert barcelona.external.source == "linebet"
    assert barcelona.external.source_event_id == _BARCELONA_CI
    assert barcelona.start_time_utc is not None
    assert barcelona.competition is not None
    assert barcelona.competition.name == "UEFA Champions League"


def test_parse_odds_btts_and_double_chance(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_odds_response())
    btts = next(quote for quote in outcome.quotes if quote.market == "btts")
    assert btts.prices == {"yes": 1.79, "no": 4.905}
    dc = next(quote for quote in outcome.quotes if quote.market == "double_chance")
    assert dc.prices == {"1X": 1.001, "12": 1.044, "X2": 8.9}


def test_parse_odds_totals_and_correct_score(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_odds_response())
    totals = {quote.market: quote.prices for quote in outcome.quotes if quote.market.startswith("total_")}
    assert "total_2_5" in totals
    assert set(totals["total_2_5"]) == {"over", "under"}
    cs = next(quote for quote in outcome.quotes if quote.market == "correct_score")
    # P = home + away/1000: 2.001 -> 2:1 and 0.002 -> 0:2 from the capture.
    assert cs.prices["2:1"] == 13.0
    assert cs.prices["0:2"] == 100.0
    assert all(value > 1.0 for value in cs.prices.values())


def test_parse_odds_warnings_are_honest(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_odds_response())
    joined = " | ".join(outcome.warnings)
    assert "not an approved execution bookmaker" in joined
    assert "HT/FT" in joined


# -- parse_events (h2h) -----------------------------------------------------


def test_parse_events_builds_meeting_rows(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_events(_assembled_h2h_response())
    # The fixture carries 11 true head-to-head meetings plus 3 team-form games;
    # only the true meetings (entity.gameIds) may be shipped as h2h events.
    assert len(outcome.events) == 11
    for event in outcome.events:
        assert event.sport == "football"
        assert event.status == "finished"
        assert {event.home.name, event.away.name} == {"Liverpool", "Atletico Madrid"}
        assert event.home.score is not None and event.away.score is not None
        assert event.external.source == "linebet"
        assert event.external.source_event_id
        assert event.competition is not None
        assert event.competition.name
    # sorted by start time ascending
    times = [event.start_time_utc or "" for event in outcome.events]
    assert times == sorted(times)


def test_parse_events_empty_history_warns(adapter: LinebetAdapter) -> None:
    body = _h2h_body()
    body["entity"]["gameIds"] = []
    payload = {
        "kind": "h2h",
        "match": _list_body()["Value"][0],
        "swapped": False,
        "payload": body,
    }
    resp = SourceResponse(
        source="linebet",
        payload=json.dumps(payload),
        url="https://linebet.com/service-api/statisticfeed/api/v1/Game/h2h",
        status=200,
        fetched_at="2026-09-09T10:00:00+00:00",
    )
    outcome = adapter.parse_events(resp)
    assert outcome.events == []
    assert any("no recorded meetings" in warning for warning in outcome.warnings)


# -- fetch ------------------------------------------------------------------


def _httpx_response(body: dict, url: str) -> httpx.Response:
    return httpx.Response(200, json=body, request=httpx.Request("GET", url))


def test_fetch_odds_assembles_list_and_details(adapter: LinebetAdapter, monkeypatch) -> None:
    responses = [_httpx_response(_list_body(), "https://linebet.com/list")]
    for _ in _list_body()["Value"]:
        responses.append(_httpx_response(_gamezip_body(), "https://linebet.com/gamezip"))

    def fake_request(url: str, *, params: dict[str, str]) -> httpx.Response:
        return responses.pop(0)

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("football", "odds", {})
    assembled = json.loads(resp.payload)
    assert assembled["kind"] == "odds"
    assert assembled["payload"]["Value"]
    assert _BARCELONA_CI in assembled["details"]
    assert assembled["detail_skipped"] == []


def test_fetch_odds_linebet_category_serves_odds(adapter: LinebetAdapter, monkeypatch) -> None:
    responses = [_httpx_response(_list_body(), "https://linebet.com/list")]
    for _ in _list_body()["Value"]:
        responses.append(_httpx_response(_gamezip_body(), "https://linebet.com/gamezip"))

    def fake_request(url: str, *, params: dict[str, str]) -> httpx.Response:
        return responses.pop(0)

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("football", "odds_linebet", {})
    assembled = json.loads(resp.payload)
    assert assembled["kind"] == "odds"
    assert _BARCELONA_CI in assembled["details"]


def test_fetch_h2h_matches_pair_and_loads_history(adapter: LinebetAdapter, monkeypatch) -> None:
    responses = [
        _httpx_response(_list_body(), "https://linebet.com/list"),
        _httpx_response(_h2h_body(), "https://linebet.com/h2h"),
    ]

    def fake_request(url: str, *, params: dict[str, str]) -> httpx.Response:
        return responses.pop(0)

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("football", "h2h", {"entity_a": "Liverpool", "entity_b": "Atletico Madrid"})
    assembled = json.loads(resp.payload)
    assert assembled["kind"] == "h2h"
    assert assembled["match"]["CI"] == 364650228
    assert assembled["swapped"] is False


def test_fetch_h2h_unmatched_pair_raises_not_found(adapter: LinebetAdapter, monkeypatch) -> None:
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda url, *, params: _httpx_response(_list_body(), url),
    )
    with pytest.raises(NotFound):
        adapter.fetch("football", "h2h", {"entity_a": "Not In Slate", "entity_b": "Feyenoord"})


def test_fetch_dated_query_raises_not_found(adapter: LinebetAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.fetch("football", "odds", {"date": "2026-09-09"})


def test_fetch_non_football_raises_not_found(adapter: LinebetAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.fetch("tennis", "odds", {})


def test_fetch_h2h_missing_entities_raises_bad_request(
    adapter: LinebetAdapter, monkeypatch
) -> None:
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda url, *, params: _httpx_response(_list_body(), url),
    )
    with pytest.raises(BadRequest):
        adapter.fetch("football", "h2h", {"entity_a": "Barcelona"})
