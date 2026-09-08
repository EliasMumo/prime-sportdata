"""Offline Betika adapter tests against live-captured fixtures (2026-09-08).

The JSON fixtures under ``tests/fixtures/`` are trimmed rows from real HTTP
200 responses captured on 2026-09-08 (see ``docs/source-health.md``). No
network is touched here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prime_sportdata.errors import NotFound
from prime_sportdata.models import OddsQuote
from prime_sportdata.sources.base import SourceResponse
from prime_sportdata.sources.betika import (
    BetikaAdapter,
    _start_time_utc,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _assembled_response() -> SourceResponse:
    rows = json.loads((FIXTURES / "betika_matches.json").read_text(encoding="utf-8"))
    detail = json.loads((FIXTURES / "betika_match_detail.json").read_text(encoding="utf-8"))
    payload = json.dumps({"rows": rows["data"], "details": {"73159940": detail}})
    return SourceResponse(
        source="betika",
        payload=payload,
        url="https://api.betika.com/v1/uo/matches",
        status=200,
        fetched_at="2026-09-08T10:00:00+00:00",
    )


@pytest.fixture()
def adapter() -> BetikaAdapter:
    return BetikaAdapter()


def test_parse_odds_emits_1x2_htft_correct_score(adapter: BetikaAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_response())
    markets = {q.market for q in outcome.quotes}
    assert "1x2" in markets
    assert "htft" in markets
    assert "correct_score" in markets
    assert "total_2_5" in markets
    assert "double_chance" in markets


def test_parse_odds_htft_has_all_nine_outcomes(adapter: BetikaAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_response())
    htft = next(q for q in outcome.quotes if q.market == "htft")
    assert set(htft.prices) == {
        "1/1", "1/X", "1/2", "X/1", "X/X", "X/2", "2/1", "2/X", "2/2",
    }
    assert all(value > 1.0 for value in htft.prices.values())
    assert htft.bookmaker == "betika"


def test_parse_odds_correct_score_complete_grid(adapter: BetikaAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_response())
    cs = next(q for q in outcome.quotes if q.market == "correct_score")
    assert len(cs.prices) == 26
    for home in range(5):
        for away in range(5):
            assert f"{home}:{away}" in cs.prices
    assert "OTHER" in cs.prices


def test_parse_odds_metadata(adapter: BetikaAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_response())
    first = next(q for q in outcome.quotes if q.external.source_event_id == "73159940")
    assert isinstance(first, OddsQuote)
    assert first.sport == "football"
    assert first.external.source == "betika"
    assert first.home == "Al-Hazm"
    assert first.away == "Al-Taawoun FC"
    assert first.competition is not None
    assert first.competition.name == "Saudi Pro League"
    # Betika start_time is Africa/Nairobi: 2026-09-08 18:55 local -> 15:55 UTC.
    assert first.start_time_utc == "2026-09-08T15:55:00+00:00"
    assert any("Africa/Nairobi" in w for w in outcome.warnings)
    assert any("not an approved execution bookmaker" in w for w in outcome.warnings)


def test_parse_odds_non_soccer_rows_are_skipped(adapter: BetikaAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_response())
    # The fixture list includes one non-soccer row (TT Elite Series); its
    # match id must never produce quotes.
    event_ids = {q.external.source_event_id for q in outcome.quotes}
    assert "74424870" not in event_ids
    assert any("filtered caller-side to sport_name='Soccer'" in w for w in outcome.warnings)


def test_parse_events_is_not_supported(adapter: BetikaAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.parse_events(_assembled_response())


def test_fetch_wrong_sport_or_category_raises_not_found(adapter: BetikaAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.fetch("basketball", "odds_detailed", {})
    with pytest.raises(NotFound):
        adapter.fetch("football", "odds", {})
    with pytest.raises(NotFound):
        adapter.fetch("football", "odds_detailed", {"date": "2026-09-08"})


def test_start_time_utc_conversion() -> None:
    assert _start_time_utc("2026-09-08 18:50:00") == "2026-09-08T15:50:00+00:00"


def test_start_time_utc_invalid_returns_none() -> None:
    assert _start_time_utc("") is None
    assert _start_time_utc(None) is None
    assert _start_time_utc("not-a-time") is None


def test_complete_score_map_requires_grid_and_other(adapter: BetikaAdapter) -> None:
    grid = {f"{h}:{a}": 8.0 for h in range(5) for a in range(5)}
    grid["OTHER"] = 24.0
    assert adapter._complete_score_map(grid) is True
    partial = dict(grid)
    del partial["OTHER"]
    assert adapter._complete_score_map(partial) is False


def test_market_map_ignores_unsupported_groups(adapter: BetikaAdapter) -> None:
    detail = {
        "data": [
            {"sub_type_id": "60", "name": "1ST HALF - 1X2", "odds": [
                {"display": "1", "odd_value": "1.5"}
            ]},
            {"sub_type_id": "47", "name": "HALFTIME/FULLTIME", "odds": [
                {"display": "1/1", "odd_value": "5.6"}
            ]},
        ]
    }
    markets = adapter._market_map(detail)
    assert "60" not in markets
    assert "47" in markets
