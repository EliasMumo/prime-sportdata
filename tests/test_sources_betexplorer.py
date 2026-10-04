"""Offline BetExplorer adapter tests against live-captured fixtures (2026-09-08).

The HTML fixtures under ``tests/fixtures/`` are trimmed rows from real HTTP
200 responses captured on 2026-09-08 (see ``docs/source-health.md``). No
network is touched here.
"""

from pathlib import Path

import pytest

from prime_sportdata.errors import NotFound
from prime_sportdata.models import OddsQuote
from prime_sportdata.sources.base import SourceResponse
from prime_sportdata.sources.betexplorer import (
    BetexplorerAdapter,
    _parse_kickoff_utc,
    _parse_tournament,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _response(sport: str) -> SourceResponse:
    html = (FIXTURES / f"betexplorer_{sport}.html").read_text(encoding="utf-8")
    return SourceResponse(
        source="betexplorer",
        payload=html,
        url=f"https://www.betexplorer.com/{sport}/",
        status=200,
        fetched_at="2026-09-08T08:00:00+00:00",
    )


@pytest.fixture()
def adapter() -> BetexplorerAdapter:
    return BetexplorerAdapter()


def test_parse_odds_football_1x2(adapter: BetexplorerAdapter) -> None:
    outcome = adapter.parse_odds(_response("football"))
    assert len(outcome.quotes) == 4
    first = outcome.quotes[0]
    assert isinstance(first, OddsQuote)
    assert first.market == "1x2"
    assert first.prices == {"1": 1.13, "X": 6.80, "2": 12.70}
    assert first.home == "Negeri Sembilan"
    assert first.away == "Kelantan City"
    assert first.external.source == "betexplorer"
    assert first.external.source_event_id == "E50wDhIl"
    assert first.competition is not None
    assert first.competition.name == "FA Cup"
    assert first.competition.country == "Malaysia"
    assert first.bookmaker == "betexplorer-default"
    # Prague is UTC+2 in September: 09:45 local -> 07:45 UTC.
    assert first.start_time_utc == "2026-09-08T07:45:00+00:00"
    # Standing honesty warnings are attached.
    assert any("Europe/Prague" in w for w in outcome.warnings)
    assert any("default odds display" in w for w in outcome.warnings)


def test_parse_odds_basketball_two_way(adapter: BetexplorerAdapter) -> None:
    outcome = adapter.parse_odds(_response("basketball"))
    assert len(outcome.quotes) == 6
    first = outcome.quotes[0]
    assert first.market == "home_away"
    assert set(first.prices) == {"home", "away"}
    assert first.prices == {"home": 1.73, "away": 1.97}
    assert first.home == "Yokohama"
    assert first.away == "Yokohama Excellence"


def test_parse_odds_tennis_two_way(adapter: BetexplorerAdapter) -> None:
    outcome = adapter.parse_odds(_response("tennis"))
    assert len(outcome.quotes) == 6
    first = outcome.quotes[0]
    assert first.market == "home_away"
    assert first.prices == {"home": 6.80, "away": 1.05}
    assert first.home == "Amarandei D. A."
    assert first.away == "McHugh A."


def test_parse_odds_skips_rows_with_wrong_odds_count(adapter: BetexplorerAdapter) -> None:
    """A football row with a two-way odds column must be skipped, not guessed."""
    html = (
        "<!DOCTYPE html><html><body><table>"
        '<tr data-dt="8,9,2026,12,0"><td class="h-text-left">'
        '<span class="table-main__time">12:00</span>'
        '<a href="/football/england/premier-league/a-b/E50wDhIl/">A - B</a></td>'
        '<td class="table-main__odds"><button data-odd="1.80"></button></td>'
        '<td class="table-main__odds"><button data-odd="2.10"></button></td>'
        "</tr></table></body></html>"
    )
    resp = SourceResponse(
        source="betexplorer",
        payload=html,
        url="https://www.betexplorer.com/football/",
        status=200,
        fetched_at="2026-09-08T08:00:00+00:00",
    )
    outcome = adapter.parse_odds(resp)
    assert outcome.quotes == []
    assert any("expected 3 odds" in w for w in outcome.warnings)


def test_parse_odds_skips_invalid_decimal_price(adapter: BetexplorerAdapter) -> None:
    html = (
        "<!DOCTYPE html><html><body><table>"
        '<tr data-dt="8,9,2026,12,0"><td class="h-text-left">'
        '<span class="table-main__time">12:00</span>'
        '<a href="/football/england/premier-league/a-b/E50wDhIl/">A - B</a></td>'
        '<td class="table-main__odds"><button data-odd="1.80"></button></td>'
        '<td class="table-main__odds"><button data-odd="0.90"></button></td>'
        '<td class="table-main__odds"><button data-odd="2.10"></button></td>'
        "</tr></table></body></html>"
    )
    resp = SourceResponse(
        source="betexplorer",
        payload=html,
        url="https://www.betexplorer.com/football/",
        status=200,
        fetched_at="2026-09-08T08:00:00+00:00",
    )
    outcome = adapter.parse_odds(resp)
    assert outcome.quotes == []
    assert any("not a valid decimal" in w for w in outcome.warnings)


def test_parse_events_is_not_supported(adapter: BetexplorerAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.parse_events(_response("football"))


def test_fetch_non_odds_category_raises_not_found(adapter: BetexplorerAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.fetch("football", "fixtures", {"date": "2026-09-08"})


def test_fetch_other_date_raises_not_found(adapter: BetexplorerAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.fetch("football", "odds", {"date": "2026-09-01"})


def test_fetch_unknown_sport_raises_bad_request(adapter: BetexplorerAdapter) -> None:
    from prime_sportdata.errors import BadRequest

    with pytest.raises(BadRequest):
        adapter.fetch("cricket", "odds", {})


def test_parse_kickoff_utc_conversion() -> None:
    assert _parse_kickoff_utc("8,9,2026,9,45") == "2026-09-08T07:45:00+00:00"


def test_parse_kickoff_utc_invalid_returns_none() -> None:
    assert _parse_kickoff_utc("not-a-date") is None
    assert _parse_kickoff_utc("") is None


def test_parse_tournament_with_country() -> None:
    row = (
        '<tr class="js-tournament"><th><a href="/football/malaysia/fa-cup/" '
        'class="table-main__tournament">Malaysia: FA Cup</a></th></tr>'
    )
    comp = _parse_tournament(row)
    assert comp is not None
    assert comp.name == "FA Cup"
    assert comp.country == "Malaysia"


def test_parse_tournament_without_country() -> None:
    row = (
        '<tr class="js-tournament"><th><a href="/football/x/y/" '
        'class="table-main__tournament">Friendlies</a></th></tr>'
    )
    comp = _parse_tournament(row)
    assert comp is not None
    assert comp.name == "Friendlies"
    assert comp.country is None


def test_parse_tournament_missing_returns_none() -> None:
    assert _parse_tournament("<tr><td>no anchor</td></tr>") is None


# --- over/under detail path (live-verified 2026-10-04) -----------------------


def _ou_response(odds_html: str, params: dict[str, str] | None = None) -> SourceResponse:
    import json

    payload = json.dumps({"odds": odds_html})
    return SourceResponse(
        source="betexplorer",
        payload=payload,
        url="https://www.betexplorer.com/match-odds/8p61npDl/1/ou/bestOdds/?lang=en",
        status=200,
        fetched_at="2026-10-04T07:30:00+00:00",
        params=params or {"event": "8p61npDl", "home": "Newells Old Boys", "away": "Lanus"},
    )


def _ou_bookmaker_row(bookmaker: str, line: str, over: str, under: str) -> str:
    return (
        '<tr data-bo="true" data-bid="417" data-bookie-id="1039">'
        f'<td class="h-text-left over-s-only h-text-pl10">'
        f'<a href="/bookmaker/1039/redirect">{bookmaker}</a></td>'
        '<td class="h-text-left under-s-only h-text-pl10"></td>'
        '<td class="h-text-left tablet-desktop-only"></td>'
        f'<td class="table-main__doubleparameter">{line}</td>'
        f'<td class="table-main__detail-odds" data-odd="{over}"></td>'
        f'<td class="table-main__detail-odds" data-odd="{under}"></td>'
        "</tr>"
    )


def test_parse_ou_odds_normalizes_bookmaker_rows(adapter: BetexplorerAdapter) -> None:
    html = (
        "<table>"
        + _ou_bookmaker_row("1xBet", "2.5", "2.74", "1.45")
        + _ou_bookmaker_row("22Bet", "2.5", "2.74", "1.45")
        + _ou_bookmaker_row("Stake.com", "0.5", "1.12", "6.00")
        + "</table>"
    )
    outcome = adapter.parse_odds(_ou_response(html))
    assert len(outcome.quotes) == 3
    first = outcome.quotes[0]
    assert first.market == "total_2_5"
    assert first.prices == {"over": 2.74, "under": 1.45}
    assert first.bookmaker == "1xBet"
    assert first.home == "Newells Old Boys"
    assert first.away == "Lanus"
    assert first.external.source == "betexplorer"
    assert first.external.source_event_id == "8p61npDl"
    assert first.external.source_url.endswith("/1/ou/bestOdds/?lang=en")
    assert first.start_time_utc is None
    assert outcome.quotes[2].market == "total_0_5"
    assert outcome.quotes[2].prices == {"over": 1.12, "under": 6.00}
    assert any("AJAX endpoint" in w for w in outcome.warnings)
    assert any("home/away come from" in w for w in outcome.warnings)


def test_parse_ou_odds_skips_malformed_rows(adapter: BetexplorerAdapter) -> None:
    html = (
        "<table>"
        + _ou_bookmaker_row("1xBet", "2.5", "2.74", "1.45")
        # wrong price count: parsed prices length != 2
        + '<tr><td class="table-main__doubleparameter">2.5</td>'
        '<td data-odd="1.80"></td><td data-odd="0.90"></td><td data-odd="2.10"></td></tr>'
        # invalid price <= 1.0
        + _ou_bookmaker_row("BadBook", "2.5", "0.90", "1.45")
        # unusable line
        + _ou_bookmaker_row("OddBook", "two and a half", "2.74", "1.45")
        + "</table>"
    )
    outcome = adapter.parse_odds(_ou_response(html))
    assert len(outcome.quotes) == 1
    assert outcome.quotes[0].bookmaker == "1xBet"
    assert any("found 3" in w for w in outcome.warnings)
    assert any("not a valid decimal" in w for w in outcome.warnings)
    assert any("unusable total line" in w for w in outcome.warnings)


def test_parse_ou_odds_rejects_non_json_payload(adapter: BetexplorerAdapter) -> None:
    from prime_sportdata.errors import NoData

    resp = SourceResponse(
        source="betexplorer",
        payload="<html>not json</html>",
        url="https://www.betexplorer.com/match-odds/8p61npDl/1/ou/bestOdds/",
        status=200,
        fetched_at="2026-10-04T07:30:00+00:00",
        params={"event": "8p61npDl", "home": "A", "away": "B"},
    )
    with pytest.raises(NoData):
        adapter.parse_odds(resp)


def test_ou_line_key_conversions() -> None:
    from prime_sportdata.sources.betexplorer import _ou_line_key

    assert _ou_line_key("0.5") == "0_5"
    assert _ou_line_key("2") == "2_0"
    assert _ou_line_key("2.5") == "2_5"
    assert _ou_line_key("3.5") == "3_5"
    assert _ou_line_key("2.25") == "2_25"
    assert _ou_line_key("2.75") == "2_75"
    assert _ou_line_key("2.20") is None  # not quarter-aligned
    assert _ou_line_key("abc") is None
    assert _ou_line_key("0") is None
    assert _ou_line_key("99") is None


def test_fetch_ou_requires_event_and_teams(adapter: BetexplorerAdapter) -> None:
    from prime_sportdata.errors import BadRequest

    with pytest.raises(BadRequest):
        adapter.fetch("football", "ou", {})
    with pytest.raises(BadRequest):
        adapter.fetch("football", "ou", {"event": "8p61npDl"})
    with pytest.raises(BadRequest):
        adapter.fetch("football", "ou", {"event": "8p61npDl", "home": "A"})
    with pytest.raises(BadRequest):
        adapter.fetch("football", "ou", {"event": "8p61npDl", "away": "B"})


def test_fetch_ou_football_only(adapter: BetexplorerAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.fetch("basketball", "ou", {"event": "x", "home": "A", "away": "B"})
