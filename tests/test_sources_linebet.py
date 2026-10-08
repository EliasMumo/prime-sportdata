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
from prime_sportdata.sources.linebet import (
    BetwinnerAdapter,
    LinebetAdapter,
    OneXBetKeAdapter,
)

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


def _betwinner_list_body() -> dict:
    return json.loads((FIXTURES / "betwinner_matches.json").read_text(encoding="utf-8"))


def _betwinner_gamezip_body() -> dict:
    return json.loads((FIXTURES / "betwinner_gamezip.json").read_text(encoding="utf-8"))


def _betwinner_assembled_odds_response() -> SourceResponse:
    payload = {
        "kind": "odds",
        "payload": _betwinner_list_body(),
        "details": {str(325699846): _betwinner_gamezip_body()["Value"]},
        "detail_skipped": [],
    }
    return SourceResponse(
        source="betwinner",
        payload=json.dumps(payload),
        url="https://betwinner.com/service-api/LineFeed/Get1x2_VZip",
        status=200,
        fetched_at="2026-09-24T10:00:00+00:00",
    )


def _basketball_list_body() -> dict:
    """Trimmed row from a live linebet basketball capture (2026-10-07)."""
    return {
        "Value": [
            {
                "O1": "CSKA Moscow",
                "O2": "Uralmash",
                "L": "VTB United League",
                "LE": "VTB United League",
                "CN": "Russia",
                "CI": 376389401,
                "S": 1791385200,
                "E": [
                    {"T": 8, "P": -25.5, "C": 1.86},
                    {"T": 7, "P": 25.5, "C": 1.94},
                    {"T": 401, "P": None, "C": 1.06},
                    {"T": 402, "P": None, "C": 9.1},
                ],
            },
            {
                "O1": "Zenit Saint-Petersburg",
                "O2": "Parma Perm",
                "L": "VTB United League",
                "LE": "VTB United League",
                "CN": "Russia",
                "CI": 376389402,
                "S": 1791388800,
                "E": [
                    {"T": 401, "P": None, "C": 1.456},
                    {"T": 402, "P": None, "C": 2.725},
                ],
            },
        ]
    }


def _tennis_list_body() -> dict:
    """Trimmed row from a live linebet tennis capture (2026-10-07)."""
    return {
        "Value": [
            {
                "O1": "Cori Gauff",
                "O2": "Elise Mertens",
                "L": "WTA. Beijing",
                "LE": "WTA. Beijing",
                "CN": "China",
                "CI": 376710212,
                "S": 1791355500,
                "E": [
                    {"T": 1, "P": None, "C": 1.235},
                    {"T": 3, "P": None, "C": 3.98},
                    {"T": 12, "P": 2.5, "C": 1.68},
                ],
            }
        ]
    }


def _assembled_basketball_odds_response() -> SourceResponse:
    payload = {
        "kind": "odds",
        "sport": "basketball",
        "payload": _basketball_list_body(),
        "details": {},
        "half_details": {},
        "detail_skipped": [],
    }
    return SourceResponse(
        source="linebet",
        payload=json.dumps(payload),
        url="https://linebet.com/service-api/LineFeed/Get1x2_VZip",
        status=200,
        fetched_at="2026-10-07T09:00:00+00:00",
    )


def _assembled_tennis_odds_response() -> SourceResponse:
    payload = {
        "kind": "odds",
        "sport": "tennis",
        "payload": _tennis_list_body(),
        "details": {},
        "half_details": {},
        "detail_skipped": [],
    }
    return SourceResponse(
        source="linebet",
        payload=json.dumps(payload),
        url="https://linebet.com/service-api/LineFeed/Get1x2_VZip",
        status=200,
        fetched_at="2026-10-07T09:00:00+00:00",
    )


# -- 1xBet-family siblings (probe-verified 2026-09-24) ---------------------


def test_betwinner_parse_odds_emits_quotes_with_betwinner_bookmaker() -> None:
    adapter = BetwinnerAdapter(clock=lambda: 0.0, sleep=lambda _: None)
    outcome = adapter.parse_odds(_betwinner_assembled_odds_response())

    assert outcome.quotes
    assert {quote.bookmaker for quote in outcome.quotes} == {"betwinner"}
    assert "1x2" in {quote.market for quote in outcome.quotes}
    assert all(quote.external.source == "betwinner" for quote in outcome.quotes)


def test_family_sibling_rejects_h2h_without_a_verified_path() -> None:
    adapter = BetwinnerAdapter(clock=lambda: 0.0, sleep=lambda _: None)
    with pytest.raises(NotFound, match="h2h is unverified"):
        adapter.fetch("football", "h2h", {"entity_a": "Team A", "entity_b": "Team B"})


def test_family_siblings_serve_their_explicit_odds_categories() -> None:
    betwinner = BetwinnerAdapter(clock=lambda: 0.0, sleep=lambda _: None)
    assert "odds_betwinner" in betwinner.slate_categories
    onexbet = OneXBetKeAdapter(clock=lambda: 0.0, sleep=lambda _: None)
    assert "odds_1xbet_ke" in onexbet.slate_categories
    assert "odds_betwinner" not in onexbet.slate_categories


def test_catalog_exposes_the_new_family_rows() -> None:
    from prime_sportdata.catalog import default_source_order

    assert default_source_order("football", "odds_betwinner") == ("betwinner",)
    assert default_source_order("football", "odds_1xbet_ke") == ("1xbet_ke",)
    assert "betwinner" in default_source_order("football", "odds")


def test_catalog_exposes_basketball_and_tennis_linebet_rows() -> None:
    from prime_sportdata.catalog import default_source_order

    assert default_source_order("basketball", "odds_linebet") == ("linebet",)
    assert default_source_order("tennis", "odds_linebet") == ("linebet",)


def test_parse_odds_basketball_emits_home_away(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_basketball_odds_response())

    by_home = {
        quote.home: quote for quote in outcome.quotes if quote.market == "home_away"
    }
    assert set(by_home) == {"CSKA Moscow", "Zenit Saint-Petersburg"}
    cska = by_home["CSKA Moscow"]
    assert cska.prices == {"home": 1.06, "away": 9.1}
    assert cska.sport == "basketball"
    assert cska.bookmaker == "linebet"
    assert cska.external.source == "linebet"
    assert cska.start_time_utc is not None


def test_parse_odds_tennis_emits_home_away(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_tennis_odds_response())

    home_away = [quote for quote in outcome.quotes if quote.market == "home_away"]
    assert len(home_away) == 1
    assert home_away[0].prices == {"home": 1.235, "away": 3.98}
    assert home_away[0].sport == "tennis"
    assert home_away[0].home == "Cori Gauff"


def test_parse_odds_basketball_ignores_football_only_markets(
    adapter: LinebetAdapter,
) -> None:
    """Handicap/totals columns on the list row must not leak as quotes."""
    outcome = adapter.parse_odds(_assembled_basketball_odds_response())
    assert {quote.market for quote in outcome.quotes} == {"home_away"}


# -- parse_odds -------------------------------------------------------------


def test_parse_odds_emits_verified_markets_only(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_odds_response())
    markets = {quote.market for quote in outcome.quotes}
    assert "1x2" in markets
    assert "double_chance" in markets
    assert "btts" in markets
    assert "correct_score" in markets
    assert any(market.startswith("total_") for market in markets)
    # The captured fixture predates the 2026-10-08 HT/FT decoder and carries
    # no group-89 rows, so htft is absent here; the dedicated test below
    # covers the decoder itself.
    assert "htft" not in markets


def test_htft_prices_decodes_group_89(adapter: LinebetAdapter) -> None:
    from prime_sportdata.sources.linebet import _htft_prices

    rows = []
    odds = 0
    for half_type, half_key in ((763, "1"), (764, "X"), (765, "2")):
        for full_param, full_key in ((1, "1"), (2, "X"), (3, "2")):
            odds += 1
            rows.append({"T": half_type, "P": full_param, "C": 1.0 + odds / 10.0})
    # 1/1..2/2 in row order; the decoder maps each to its betika-style key.
    prices = _htft_prices({"E": [rows]})
    assert prices == {
        "1/1": 1.1, "1/X": 1.2, "1/2": 1.3,
        "X/1": 1.4, "X/X": 1.5, "X/2": 1.6,
        "2/1": 1.7, "2/X": 1.8, "2/2": 1.9,
    }
    assert _htft_prices({"E": []}) is None
    assert _htft_prices({"E": [[{"T": 763, "P": 1, "C": 1.1}]]}) is None


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
    # 180 = yes / 181 = no; the P-parameterized rows are 2nd-half variants and
    # must not leak into the regular-time market (fixed 2026-10-04).
    assert btts.prices == {"yes": 1.79, "no": 1.937}
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
    assert "11412" in joined


# -- halves (live-verified 2026-10-04) --------------------------------------

_GREECE_GERMANY_CI = "373692368"


def _assembled_halves_response() -> SourceResponse:
    event = {
        "CI": int(_GREECE_GERMANY_CI),
        "O1": "Greece",
        "O2": "Germany",
        "S": 1791139500,
        "LE": "UEFA Nations League",
        "L": "UEFA Nations League",
        "E": [
            {"T": 1, "C": 4.16},
            {"T": 2, "C": 4.0},
            {"T": 3, "C": 1.775},
        ],
    }
    payload = {
        "kind": "odds",
        "payload": {"Value": [event]},
        "details": {},
        "half_details": {
            _GREECE_GERMANY_CI: {
                "first_half": _first_half_body()["Value"],
                "second_half": _second_half_body()["Value"],
            }
        },
        "detail_skipped": [],
    }
    return SourceResponse(
        source="betwinner",
        payload=json.dumps(payload),
        url="https://betwinner.com/service-api/LineFeed/Get1x2_VZip",
        status=200,
        fetched_at="2026-10-04T14:45:00+00:00",
    )


def test_halves_from_main_payload_decode_verified_subgames() -> None:
    from prime_sportdata.sources.linebet import _halves_from_main_payload

    # betwinner_gamezip.json (live capture 2026-09-24) already carries the
    # half market subgames in its SG list (TG empty, PN 1st/2nd half).
    halves = _halves_from_main_payload(_betwinner_gamezip_body()["Value"])
    assert halves == {"first_half": "325699848", "second_half": "325699852"}


def _first_half_body() -> dict:
    return json.loads((FIXTURES / "linebet_first_half_gamezip.json").read_text(encoding="utf-8"))


def _second_half_body() -> dict:
    return json.loads((FIXTURES / "linebet_second_half_gamezip.json").read_text(encoding="utf-8"))


def test_parse_odds_ships_first_and_second_half_markets() -> None:
    adapter = BetwinnerAdapter(clock=lambda: 0.0, sleep=lambda _: None)
    outcome = adapter.parse_odds(_assembled_halves_response())
    by_market = {quote.market: quote for quote in outcome.quotes}
    # 1st-half and 2nd-half 1X2 verified against the site's rendered halves
    # for this feed (Greece-Germany capture 2026-10-04).
    assert by_market["first_half_1x2"].prices == {"1": 4.35, "X": 2.375, "2": 2.29}
    assert by_market["second_half_1x2"].prices == {"1": 4.1, "X": 2.76, "2": 2.08}
    assert by_market["first_half_double_chance"].prices == {"1X": 1.54, "12": 1.51, "X2": 1.168}
    assert by_market["second_half_double_chance"].prices == {"1X": 1.66, "12": 1.39, "X2": 1.188}
    assert by_market["first_half_btts"].prices == {"yes": 3.72, "no": 1.239}
    assert by_market["second_half_btts"].prices == {"yes": 2.84, "no": 1.382}
    # Totals and team totals ship with the same line encoding as betika.
    assert "first_half_total_0_5" in by_market
    assert "second_half_total_0_5" in by_market
    assert "first_half_home_total_0_5" in by_market
    assert "second_half_away_total_0_5" in by_market
    assert set(by_market["first_half_total_0_5"].prices) == {"over", "under"}
    for market, quote in by_market.items():
        if market.startswith(("first_half", "second_half")):
            assert quote.bookmaker == "betwinner"
            assert quote.external.source_event_id == _GREECE_GERMANY_CI


def test_parse_odds_without_halves_stays_unchanged(adapter: LinebetAdapter) -> None:
    outcome = adapter.parse_odds(_assembled_odds_response())
    markets = {quote.market for quote in outcome.quotes}
    assert not any(market.startswith(("first_half", "second_half")) for market in markets)


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

    def fake_request(url: str, *, params: dict[str, str], referer=None) -> httpx.Response:
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

    def fake_request(url: str, *, params: dict[str, str], referer=None) -> httpx.Response:
        return responses.pop(0)

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("football", "odds_linebet", {})
    assembled = json.loads(resp.payload)
    assert assembled["kind"] == "odds"
    assert _BARCELONA_CI in assembled["details"]


def test_fetch_odds_stops_details_when_budget_exhausted(monkeypatch) -> None:
    # A slow upstream must never let the detail phase outlive the hosted
    # proxy timeout: once the injectable clock passes DETAIL_BUDGET_S, the
    # remaining rows are skipped with the budget marker.
    from prime_sportdata.sources import linebet as linebet_module

    now = 0.0
    adapter = LinebetAdapter(clock=lambda: now, sleep=lambda _: None)
    responses = [_httpx_response(_list_body(), "https://linebet.com/list")]
    for _ in _list_body()["Value"]:
        responses.append(_httpx_response(_gamezip_body(), "https://linebet.com/gamezip"))

    def fake_request(url: str, *, params: dict[str, str], referer=None) -> httpx.Response:
        nonlocal now
        response = responses.pop(0)
        now += linebet_module.DETAIL_BUDGET_S  # one detail request consumed it all
        return response

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("football", "odds_linebet", {})
    assembled = json.loads(resp.payload)
    assert assembled["detail_skipped"] and "detail-budget" in assembled["detail_skipped"]
    assert _BARCELONA_CI in assembled["details"]


def test_fetch_h2h_matches_pair_and_loads_history(adapter: LinebetAdapter, monkeypatch) -> None:
    responses = [
        _httpx_response(_list_body(), "https://linebet.com/list"),
        _httpx_response(_h2h_body(), "https://linebet.com/h2h"),
    ]

    def fake_request(url: str, *, params: dict[str, str], referer=None) -> httpx.Response:
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
        lambda url, *, params, referer=None: _httpx_response(_list_body(), url),
    )
    with pytest.raises(NotFound):
        adapter.fetch("football", "h2h", {"entity_a": "Not In Slate", "entity_b": "Feyenoord"})


def test_fetch_dated_query_raises_not_found(adapter: LinebetAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.fetch("football", "odds", {"date": "2026-09-09"})


def test_fetch_unknown_sport_raises_not_found(adapter: LinebetAdapter) -> None:
    with pytest.raises(NotFound):
        adapter.fetch("cricket", "odds", {})


def test_fetch_basketball_odds_uses_sports_3_and_skips_details(
    adapter: LinebetAdapter, monkeypatch
) -> None:
    seen: dict[str, object] = {}

    def fake_request(url: str, *, params: dict[str, str], referer=None) -> httpx.Response:
        seen["params"] = params
        seen["referer"] = referer
        seen["detail_calls"] = seen.get("detail_calls", 0) + 1
        return _httpx_response(_basketball_list_body(), url)

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("basketball", "odds_linebet", {})
    assembled = json.loads(resp.payload)
    assert assembled["kind"] == "odds"
    assert assembled["sport"] == "basketball"
    assert seen["params"]["sports"] == "3"
    assert seen["referer"] == "https://linebet.com/en/line/basketball"
    assert seen["detail_calls"] == 1  # only the list; no GetGameZip detail
    assert assembled["details"] == {}


def test_fetch_tennis_odds_uses_sports_4(adapter: LinebetAdapter, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(url: str, *, params: dict[str, str], referer=None) -> httpx.Response:
        seen["params"] = params
        return _httpx_response(_tennis_list_body(), url)

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("tennis", "odds_linebet", {})
    assembled = json.loads(resp.payload)
    assert assembled["sport"] == "tennis"
    assert seen["params"]["sports"] == "4"


def test_fetch_h2h_missing_entities_raises_bad_request(
    adapter: LinebetAdapter, monkeypatch
) -> None:
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda url, *, params, referer=None: _httpx_response(_list_body(), url),
    )
    with pytest.raises(BadRequest):
        adapter.fetch("football", "h2h", {"entity_a": "Barcelona"})
