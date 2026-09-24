"""Offline Betika adapter tests against live-captured fixtures (2026-09-08).

The JSON fixtures under ``tests/fixtures/`` are trimmed rows from real HTTP
200 responses captured on 2026-09-08 (see ``docs/source-health.md``). No
network is touched here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from prime_sportdata.errors import NotFound
from prime_sportdata.models import OddsQuote
from prime_sportdata.sources.base import SourceResponse
from prime_sportdata.sources.betika import (
    BetikaAdapter,
    _row_priority,
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
    assert "first_half_correct_score" in markets
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


def test_parse_odds_first_half_correct_score_grid(adapter: BetikaAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_response())
    ht_cs = next(
        q for q in outcome.quotes if q.market == "first_half_correct_score"
    )
    assert len(ht_cs.prices) == 10
    for home in range(3):
        for away in range(3):
            assert f"{home}:{away}" in ht_cs.prices
    assert "OTHER" in ht_cs.prices
    assert ht_cs.prices["0:0"] == 3.0
    assert ht_cs.bookmaker == "betika"


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


def test_priority_pairs_cover_expanded_african_and_international_leagues() -> None:
    """Pairs live-verified from the 2026-09-24 upcoming list must rank 0."""
    pairs = [
        ({"competition_name": "Africa Cup of Nations Qualification", "category": "International"}),
        ({"competition_name": "UEFA Nations League", "category": "International"}),
        ({"competition_name": "Premier League", "category": "Nigeria"}),
        ({"competition_name": "Canadian Premier League", "category": "Canada"}),
        ({"competition_name": "Gulf Cup", "category": "International"}),
        ({"competition_name": "FIFA ASEAN Cup", "category": "International"}),
        ({"competition_name": "First Division", "category": "Jordan"}),
        ({"competition_name": "2. Division A", "category": "Egypt"}),
    ]
    for row in pairs:
        assert _row_priority(row) == 0, row


def test_priority_still_rejects_egyptian_premier_league_lookalike() -> None:
    """The expanded set must not admit Egypt's senior 'Premier League'."""
    assert _row_priority({"competition_name": "Premier League", "category": "Egypt"}) == 1


def _list_row(
    match_id: str,
    start_time: str,
    home: str,
    away: str,
    competition: str,
    *,
    sport: str = "Soccer",
    country: str = "",
) -> dict:
    return {
        "parent_match_id": match_id,
        "start_time": start_time,
        "home_team": home,
        "away_team": away,
        "competition_name": competition,
        "category": country,
        "sport_name": sport,
        "home_odd": "1.90",
        "neutral_odd": "3.40",
        "away_odd": "4.20",
    }


class _FakeTransport(BetikaAdapter):
    """Adapter whose internal ``_request`` is answered from canned data."""

    def __init__(self, pages: dict[int, list[dict]], details: dict[str, dict]) -> None:
        self._sleep_calls: list[float] = []
        super().__init__(
            clock=lambda: 0.0,
            sleep=self._sleep_calls.append,
            now_utc=lambda: datetime(2026, 9, 9, 4, 0, tzinfo=UTC),
        )
        self._pages = pages
        self._details = details
        self.calls: list[tuple[str, dict[str, str]]] = []

    def _request(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        self.calls.append((url, dict(params)))
        if url.endswith("/v1/uo/matches"):
            return httpx.Response(200, json={"data": self._pages.get(int(params["page"]), [])})
        if url.endswith("/v1/uo/match"):
            return httpx.Response(200, json=self._details.get(params["parent_match_id"], {}))
        return httpx.Response(404, json={})


def test_fetch_paginates_until_horizon() -> None:
    transport = _FakeTransport(
        {
            1: [
                _list_row("v1", "2026-09-09 07:00:00", "A SRL", "B SRL", "Eredivisie SRL"),
                _list_row("m1", "2026-09-09 08:00:00", "Home FC", "Away FC", "Division 1"),
                _list_row("tt1", "2026-09-09 07:30:00", "P1", "P2", "TT Cup", sport="Table Tennis"),
            ],
            2: [
                _list_row(
                    "m2",
                    "2026-09-09 22:00:00",
                    "Liverpool FC",
                    "Atletico Madrid",
                    "UEFA Champions League",
                ),
            ],
            3: [
                _list_row("m3", "2026-09-10 20:00:00", "X", "Y", "Premier League"),
            ],
            4: [
                _list_row("m4", "2026-09-10 21:00:00", "Z", "W", "Premier League"),
            ],
        },
        {},
    )
    resp = transport.fetch("football", "odds_detailed", {})
    payload = json.loads(resp.payload)
    assert len(payload["rows"]) == 5
    # Horizon is now+36h (2026-09-10T16:00Z). Page 3 reaches past it, so
    # page 4 must never be requested.
    list_pages = [params["page"] for url, params in transport.calls if url.endswith("matches")]
    assert list_pages == ["1", "2", "3"]
    assert all(params["limit"] == "500" for url, params in transport.calls if url.endswith("matches"))


def test_fetch_details_prioritise_top_flights_and_skip_virtuals() -> None:
    pages: dict[int, list[dict]] = {
        1: [
            _list_row(
                f"minor{index}",
                "2026-09-09 08:00:00",
                f"Minor {index} Home",
                f"Minor {index} Away",
                "Division 1",
                country="Kenya",
            )
            for index in range(25)
        ]
        + [
            _list_row(
                "srl1",
                "2026-09-09 08:00:00",
                "Real Madrid SRL",
                "Inter SRL",
                "UEFA Champions League SRL",
                country="Simulated Reality League",
            ),
            # Same generic name as a model league, different country: must
            # NOT take the budget away from real model-league fixtures.
            _list_row(
                "egypt1",
                "2026-09-09 08:00:00",
                "Pyramids FC",
                "El Gouna",
                "Premier League",
                country="Egypt",
            ),
        ],
        2: [
            _list_row(
                "ucl1",
                "2026-09-09 22:00:00",
                "Liverpool FC",
                "Atletico Madrid",
                "UEFA Champions League",
                country="International Clubs",
            ),
        ],
    }
    transport = _FakeTransport(pages, {})
    resp = transport.fetch("football", "odds_detailed", {})
    payload = json.loads(resp.payload)
    detail_ids = [params["parent_match_id"] for url, params in transport.calls if url.endswith("match")]
    # The budget is bounded (MAX_DETAIL_REQUESTS); the evening top-flight
    # fixture must be inside it even though it sorts last by kickoff, and
    # virtuals must never be detailed.  The fixture here has fewer rows than
    # the raised 2026-09-23 budget, so all real rows get detailed.
    assert len(detail_ids) <= 48
    assert "ucl1" in detail_ids
    assert "srl1" not in detail_ids
    assert set(detail_ids) <= set(payload["details"])
    # Detail requests are paced >=1s apart (fake clock returns 0.0, so every
    # follow-up request must sleep the full interval).
    assert len(transport._sleep_calls) == len(detail_ids) - 1
    assert all(wait == 1.0 for wait in transport._sleep_calls)


def test_parse_odds_skips_virtual_rows(adapter: BetikaAdapter) -> None:
    rows = json.loads((FIXTURES / "betika_matches.json").read_text(encoding="utf-8"))["data"]
    detail = json.loads((FIXTURES / "betika_match_detail.json").read_text(encoding="utf-8"))
    srl_row = _list_row(
        "99999999",
        "2026-09-08 19:00:00",
        "Real Madrid SRL",
        "Inter Milan SRL",
        "UEFA Champions League SRL",
    )
    payload = json.dumps({"rows": [*rows, srl_row], "details": {"73159940": detail}})
    resp = SourceResponse(
        source="betika",
        payload=payload,
        url="https://api.betika.com/v1/uo/matches",
        status=200,
        fetched_at="2026-09-08T10:00:00+00:00",
    )
    outcome = adapter.parse_odds(resp)
    event_ids = {q.external.source_event_id for q in outcome.quotes}
    assert "99999999" not in event_ids
    assert any("virtual soccer row" in warning for warning in outcome.warnings)


def test_parse_odds_emits_detailed_rows_first(adapter: BetikaAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_response())
    assert outcome.quotes
    # The first list row (Al-Zlfe) has no fetched details; the detailed match
    # (Al-Hazm) must come first so caller-side limit truncation keeps HTFT /
    # correct-score quotes.
    assert outcome.quotes[0].external.source_event_id == "73159940"


def _evidence_detail_response() -> SourceResponse:
    """Assembled response whose detail carries the extended market groups."""
    rows = json.loads((FIXTURES / "betika_matches.json").read_text(encoding="utf-8"))["data"]
    detail = {
        "data": [
            {"sub_type_id": "29", "odds": [
                {"display": "YES", "odd_value": "1.80"},
                {"display": "NO", "odd_value": "1.95"},
            ]},
            {"sub_type_id": "18", "odds": [
                {"display": "OVER 1.5", "special_bet_value": "total=1.5", "odd_value": "1.20"},
                {"display": "UNDER 1.5", "special_bet_value": "total=1.5", "odd_value": "4.40"},
                {"display": "OVER 2.5", "special_bet_value": "total=2.5", "odd_value": "1.85"},
                {"display": "UNDER 2.5", "special_bet_value": "total=2.5", "odd_value": "1.95"},
                {"display": "OVER 3.5", "special_bet_value": "total=3.5", "odd_value": "3.10"},
                {"display": "UNDER 3.5", "special_bet_value": "total=3.5", "odd_value": "1.35"},
            ]},
            {"sub_type_id": "60", "odds": [
                {"display": "1", "odd_value": "2.60"},
                {"display": "X", "odd_value": "3.10"},
                {"display": "2", "odd_value": "2.75"},
            ]},
            {"sub_type_id": "63", "odds": [
                {"display": "1/X", "odd_value": "1.40"},
                {"display": "X/2", "odd_value": "1.45"},
                {"display": "1/2", "odd_value": "1.30"},
            ]},
            {"sub_type_id": "68", "odds": [
                {"display": "OVER 0.5", "special_bet_value": "total=0.5", "odd_value": "1.15"},
                {"display": "UNDER 0.5", "special_bet_value": "total=0.5", "odd_value": "5.00"},
            ]},
            {"sub_type_id": "15", "odds": [
                {"display": "1 BY 1", "odd_value": "3.30"},
                {"display": "X", "odd_value": "4.20"},
            ]},
            {"sub_type_id": "21", "odds": [
                {"display": "2", "odd_value": "3.60"},
                {"display": "3", "odd_value": "5.00"},
                {"display": "6+", "odd_value": "21.00"},
            ]},
            {"sub_type_id": "166", "odds": [
                {"display": "OVER 6.5", "odd_value": "1.70"},
                {"display": "UNDER 6.5", "odd_value": "2.05"},
            ]},
            {"sub_type_id": "139", "odds": [
                {"display": "OVER 1.5", "odd_value": "1.40"},
                {"display": "UNDER 1.5", "odd_value": "2.70"},
            ]},
        ]
    }
    payload = json.dumps({"rows": rows, "details": {"73159940": detail}})
    return SourceResponse(
        source="betika",
        payload=payload,
        url="https://api.betika.com/v1/uo/matches",
        status=200,
        fetched_at="2026-09-08T10:00:00+00:00",
    )


def test_parse_odds_emits_extended_evidence_markets(adapter: BetikaAdapter) -> None:
    outcome = adapter.parse_odds(_evidence_detail_response())
    markets = {q.market: q.prices for q in outcome.quotes}
    assert markets["btts"] == {"yes": 1.80, "no": 1.95}
    assert markets["total_1_5"] == {"over": 1.20, "under": 4.40}
    assert markets["total_2_5"] == {"over": 1.85, "under": 1.95}
    assert markets["total_3_5"] == {"over": 3.10, "under": 1.35}
    assert markets["first_half_1x2"] == {"1": 2.60, "X": 3.10, "2": 2.75}
    assert markets["first_half_double_chance"] == {"1X": 1.40, "X2": 1.45, "12": 1.30}
    assert markets["first_half_total_0_5"] == {"over": 1.15, "under": 5.00}
    assert markets["winning_margin"] == {"1_BY_1": 3.30, "X": 4.20}
    assert markets["exact_goals"] == {"2": 3.60, "3": 5.00, "6+": 21.00}
    assert markets["corners_total_6_5"] == {"over": 1.70, "under": 2.05}
    assert markets["bookings_total_1_5"] == {"over": 1.40, "under": 2.70}


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
