"""Sofascore adapter offline tests: fixtures parse, typed errors, IPv4 shim.

No network in this file. Fixtures under tests/fixtures/sofascore/ were recorded
live on 2026-09-02 (sidecar: tests/fixtures/sofascore/SOURCES.md). Synthetic
bodies used below for error-mapping tests are constructed inline and are
labeled synthetic — no such response was observed live this session.
"""

import json
import socket
from pathlib import Path

import pytest

from prime_sportdata.errors import (
    BadRequest,
    NoData,
    NotFound,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.sources.base import ParseOutcome, SourceResponse
from prime_sportdata.sources.sofascore import (
    SOFASCORE_IPV4_FALLBACKS,
    SofascoreAdapter,
    _map_http_outcome,
    _sofascore_getaddrinfo,
    ipv4_only,
)

FIX = Path(__file__).parent / "fixtures" / "sofascore"
FETCHED_AT = "2026-09-02T17:50:00Z"

# source URLs (verbatim from SOURCES.md)
URL_LIVE_FB = "https://www.sofascore.com/api/v1/sport/football/events/live"
URL_NEXT_FB = "https://www.sofascore.com/api/v1/team/2817/events/next/0"
URL_LAST_FB = "https://www.sofascore.com/api/v1/team/2817/events/last/0"
URL_LIVE_BB = "https://www.sofascore.com/api/v1/sport/basketball/events/live"
URL_NEXT_BB = "https://www.sofascore.com/api/v1/team/3540/events/next/0"
URL_LAST_BB = "https://www.sofascore.com/api/v1/team/3540/events/last/0"
URL_LIVE_TN = "https://www.sofascore.com/api/v1/sport/tennis/events/live"


def load(name: str, url: str) -> SourceResponse:
    return SourceResponse(
        source="sofascore",
        payload=(FIX / name).read_bytes(),
        url=url,
        status=200,
        fetched_at=FETCHED_AT,
    )


@pytest.fixture()
def adapter() -> SofascoreAdapter:
    return SofascoreAdapter()


# --- fixture parsing ----------------------------------------------------------


def test_parse_live_football(adapter: SofascoreAdapter) -> None:
    outcome = adapter.parse_events(load("football/live.json", URL_LIVE_FB))
    assert isinstance(outcome, ParseOutcome)
    assert len(outcome.events) == 3
    ev = outcome.events[0]
    assert ev.sport == "football"
    assert ev.status == "live"
    assert ev.home.name == "Sassuolo"
    assert ev.home.source_id == 2793
    assert ev.home.score == 1
    assert ev.home.current_score is True
    assert ev.away.name == "Frosinone"
    assert ev.away.score == 1
    assert ev.start_time_utc == "2026-09-02T13:00:00+00:00"
    assert ev.live_detail == "2nd half"
    assert ev.round_label == "Round of 32"
    assert ev.competition is not None
    assert ev.competition.name == "Coppa Italia"
    assert ev.competition.country == "Italy"
    assert [(l.period_label, l.home, l.away) for l in ev.score_lines] == [
        ("H1", 1, 1),
        ("H2", 0, 0),
    ]
    # provenance: stamped to the recorded response
    assert ev.external.source == "sofascore"
    assert ev.external.source_event_id == "16860534"
    assert ev.external.source_url == URL_LIVE_FB
    # halftime event has no period2 key -> only an H1 line (no invented H2)
    halftime = next(e for e in outcome.events if e.external.source_event_id == "16490448")
    assert halftime.status == "live"
    assert [l.period_label for l in halftime.score_lines] == ["H1"]
    assert halftime.away.score == 1


def test_parse_football_team_next_scheduled(adapter: SofascoreAdapter) -> None:
    outcome = adapter.parse_events(load("football/team_next.json", URL_NEXT_FB))
    assert len(outcome.events) == 3
    assert {e.status for e in outcome.events} == {"scheduled"}
    assert all(e.home.score is None for e in outcome.events)
    ev = outcome.events[1]
    assert ev.home.name == "FC Barcelona"
    assert ev.competition is not None and ev.competition.name == "UEFA Champions League"
    assert ev.competition.country is None  # UEFA is not country-scoped
    assert ev.start_time_utc is not None and ev.start_time_utc.endswith("+00:00")
    assert any("team-scoped" in w and "date filtering" in w for w in outcome.warnings)


def test_parse_football_team_last_finished(adapter: SofascoreAdapter) -> None:
    outcome = adapter.parse_events(load("football/team_last.json", URL_LAST_FB))
    assert len(outcome.events) == 3
    first = outcome.events[0]
    assert first.status == "finished"
    assert first.home.name == "Atlético Madrid"
    assert first.home.score == 4
    assert first.away.score == 0
    assert first.away.current_score is False
    assert [(l.period_label, l.home, l.away) for l in first.score_lines] == [
        ("H1", 4, 0),
        ("H2", 0, 0),
        ("FT", 4, 0),
    ]
    assert first.round_label == "Semifinals"


def test_parse_live_basketball_empty_is_zero_events_not_error(adapter: SofascoreAdapter) -> None:
    # as-recorded 2026-09-02: HTTP 200 {"events":[]} — valid-empty, zero events
    outcome = adapter.parse_events(load("basketball/live_empty.json", URL_LIVE_BB))
    assert isinstance(outcome, ParseOutcome)
    assert outcome.events == []
    assert outcome.warnings == []


def test_parse_basketball_team_last_periods(adapter: SofascoreAdapter) -> None:
    outcome = adapter.parse_events(load("basketball/team_last.json", URL_LAST_BB))
    assert len(outcome.events) == 3
    first = outcome.events[0]
    assert first.sport == "basketball"
    assert first.status == "finished"
    assert first.home.name == "Kauno Žalgiris"
    assert first.home.score == 87
    assert first.away.score == 85
    assert [(l.period_label, l.home, l.away) for l in first.score_lines] == [
        ("Q1", 25, 29),
        ("Q2", 28, 26),
        ("Q3", 15, 17),
        ("Q4", 19, 13),
    ]
    assert first.competition is not None and first.competition.name == "Euroleague"
    assert first.competition.country is None


def test_parse_basketball_team_next_scheduled(adapter: SofascoreAdapter) -> None:
    outcome = adapter.parse_events(load("basketball/team_next.json", URL_NEXT_BB))
    assert {e.status for e in outcome.events} == {"scheduled"}
    ev = outcome.events[0]
    assert ev.away.name == "Real Madrid"
    assert ev.competition is not None and ev.competition.name == "Euroleague SuperCup"


def test_parse_live_tennis_sets_and_players(adapter: SofascoreAdapter) -> None:
    outcome = adapter.parse_events(load("tennis/live.json", URL_LIVE_TN))
    assert len(outcome.events) == 4
    singles = next(e for e in outcome.events if e.external.source_event_id == "16923866")
    assert singles.sport == "tennis"
    assert singles.status == "live"
    assert singles.home.name == "Thiago Seyboth Wild"
    assert singles.away.name == "Norbert Gombos"
    assert singles.home.source_id == 161262
    assert singles.live_detail == "2nd set"
    assert singles.round_label == "Round of 16"
    # sets = games per set; in-play 2nd set is 0-0, completed 1st set 6-4
    assert [(l.period_label, l.home, l.away) for l in singles.score_lines] == [
        ("S1", 6, 4),
        ("S2", 0, 0),
    ]
    # tennis current score = sets won
    third = next(e for e in outcome.events if e.external.source_event_id == "16960481")
    assert third.home.score == 1 and third.away.score == 1
    assert [l.period_label for l in third.score_lines] == ["S1", "S2", "S3"]
    assert third.live_detail == "3rd set"
    assert third.competition is not None and third.competition.name == "Mallorca, Spain"


def test_parse_all_fixture_events_are_stamped_and_typed(adapter: SofascoreAdapter) -> None:
    total = 0
    for name, url in [
        ("football/live.json", URL_LIVE_FB),
        ("football/team_next.json", URL_NEXT_FB),
        ("football/team_last.json", URL_LAST_FB),
        ("basketball/live_empty.json", URL_LIVE_BB),
        ("basketball/team_next.json", URL_NEXT_BB),
        ("basketball/team_last.json", URL_LAST_BB),
        ("tennis/live.json", URL_LIVE_TN),
    ]:
        for ev in adapter.parse_events(load(name, url)).events:
            total += 1
            assert ev.external.source == "sofascore"
            assert ev.external.source_url == url
            assert ev.sport in ("football", "basketball", "tennis")
            assert ev.status in (
                "scheduled",
                "live",
                "finished",
                "postponed",
                "cancelled",
                "interrupted",
            )
            assert ev.home.name and ev.away.name
    assert total == 3 + 3 + 3 + 0 + 3 + 3 + 4


def test_parse_404_error_body_raises_no_data(adapter: SofascoreAdapter) -> None:
    resp = load("errors/not_found_scheduled.json", "https://www.sofascore.com/api/v1/not/a/path")
    with pytest.raises(NoData):
        adapter.parse_events(resp)


def test_parse_non_json_payload_raises_no_data(adapter: SofascoreAdapter) -> None:
    resp = SourceResponse(
        source="sofascore", payload=b"<html>not json</html>", url="https://x/", status=200,
        fetched_at=FETCHED_AT,
    )
    with pytest.raises(NoData):
        adapter.parse_events(resp)


# --- fetch-layer typed-error mapping (synthetic bodies, offline) --------------


def outcome_for(status: int, body: bytes = b'{"events": []}') -> SourceResponse:
    return _map_http_outcome(
        "https://www.sofascore.com/api/v1/some/url", status, body, FETCHED_AT, "sofascore"
    )


def test_map_403_to_source_blocked() -> None:
    with pytest.raises(SourceBlocked) as excinfo:
        outcome_for(403)
    assert excinfo.value.source == "sofascore"
    assert excinfo.value.detail.startswith("HTTP 403")


def test_map_429_to_rate_limited() -> None:
    with pytest.raises(RateLimited) as excinfo:
        outcome_for(429)
    assert excinfo.value.code == "rate_limited"


def test_map_404_to_not_found() -> None:
    with pytest.raises(NotFound) as excinfo:
        outcome_for(404, (FIX / "errors" / "not_found_scheduled.json").read_bytes())
    assert excinfo.value.detail.startswith("HTTP 404")


def test_map_400_to_bad_request() -> None:
    with pytest.raises(BadRequest):
        outcome_for(400)


def test_map_5xx_to_source_unavailable() -> None:
    with pytest.raises(SourceUnavailable) as excinfo:
        outcome_for(500)
    assert excinfo.value.detail.startswith("HTTP 500")


def test_map_200_html_to_source_blocked() -> None:
    # synthetic challenge body: no HTML challenge was observed live this session
    html = b"<!doctype html><html><body>checking your browser</body></html>"
    with pytest.raises(SourceBlocked) as excinfo:
        outcome_for(200, html)
    assert "challenge" in excinfo.value.detail


def test_map_200_json_to_source_response() -> None:
    resp = outcome_for(200, b'{"events": []}')
    assert isinstance(resp, SourceResponse)
    assert resp.status == 200
    assert resp.payload == b'{"events": []}'


# --- IPv4-forcing shim (unit, no network) --------------------------------------


def fake_getaddrinfo(host, port, *args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port)),
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port)),
    ]


def test_shim_force_ipv4_fallback_first_and_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prime_sportdata.sources.sofascore._real_getaddrinfo", fake_getaddrinfo)
    results = _sofascore_getaddrinfo("www.sofascore.com", 443, 0, socket.SOCK_STREAM)
    assert all(r[0] == socket.AF_INET for r in results)
    assert results[0][4] == (SOFASCORE_IPV4_FALLBACKS[0], 443)
    ips = [r[4][0] for r in results]
    assert "151.101.175.52" in ips
    assert len(ips) == len(set(ips))  # deduplicated
    assert results[1][4][0] == "www.sofascore.com"  # live DNS result follows


def test_shim_keyword_family_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prime_sportdata.sources.sofascore._real_getaddrinfo", fake_getaddrinfo)
    results = _sofascore_getaddrinfo("api.sofascore.com", 443, family=socket.AF_UNSPEC)
    assert all(r[0] == socket.AF_INET for r in results)
    assert results[0][4][0] == SOFASCORE_IPV4_FALLBACKS[0]


def test_shim_ignores_non_sofascore_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    monkeypatch.setattr(
        "prime_sportdata.sources.sofascore._real_getaddrinfo",
        lambda *a, **kw: seen.append((a, kw)) or [],
    )
    _sofascore_getaddrinfo("www.flashscore.com", 443, 0, socket.SOCK_STREAM)
    args, _kwargs = seen[0]
    assert args[2] == 0  # family passed through untouched (AF_UNSPEC kept)


def test_ipv4_only_context_restores_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    original = socket.getaddrinfo
    with ipv4_only():
        assert socket.getaddrinfo is not original
    assert socket.getaddrinfo is original


# --- entity resolution helpers ------------------------------------------------


def test_pick_team_entity_prefers_exact_match() -> None:
    from prime_sportdata.sources.sofascore import SofascoreAdapter

    entities = [
        {"id": 5250, "name": "Barcelona SC Guayaquil"},
        {"id": 2817, "name": "FC Barcelona"},
        {"id": 77889, "name": "Fútbol Club Barcelona"},
    ]
    assert SofascoreAdapter._pick_team_entity(entities, "FC Barcelona")["id"] == 2817
    assert SofascoreAdapter._pick_team_entity(entities, "barcelona")["id"] == 2817
    assert SofascoreAdapter._pick_team_entity(entities, "barcelona sc")["id"] == 5250


def test_resolve_team_uses_only_type0_sport_matching_entities(
    adapter: SofascoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search responses mixing sports/players must not resolve a wrong sport."""
    payload = {
        "results": [
            {"entity": {"id": 2829, "name": "Real Madrid", "type": 0, "sport": {"slug": "football"}}},
            {"entity": {"id": 3540, "name": "Real Madrid", "type": 0, "sport": {"slug": "basketball"}}},
            {"entity": {"id": 999, "name": "Real Madrid CF", "type": None, "sport": None}},
        ]
    }
    def fake_request(url: str) -> SourceResponse:
        assert "q=q=" not in url  # regression guard: no double-encoded query
        assert "q=Real%20Madrid" in url or "q=Real+Madrid" in url
        return SourceResponse(
            source="sofascore", payload=json.dumps(payload), url=url, status=200,
            fetched_at=FETCHED_AT,
        )

    monkeypatch.setattr(adapter, "_request", fake_request)
    assert adapter._resolve_team("basketball", "Real Madrid") == 3540
    with pytest.raises(NoData):
        adapter._resolve_team("tennis", "Real Madrid")  # no tennis entity present


def test_fetch_no_team_raises_not_found_with_evidence(adapter: SofascoreAdapter) -> None:
    with pytest.raises(NotFound) as excinfo:
        adapter.fetch("football", "fixtures", {"date": "2026-09-02"})
    assert "scheduled-events" in excinfo.value.detail
    assert excinfo.value.source == "sofascore"


def test_fetch_tennis_raises_not_found(adapter: SofascoreAdapter) -> None:
    with pytest.raises(NotFound) as excinfo:
        adapter.fetch("tennis", "results", {"team": "Alcaraz"})
    assert "tennis" in excinfo.value.detail


def test_fetch_h2h_raises_not_found_with_evidence(adapter: SofascoreAdapter) -> None:
    with pytest.raises(NotFound) as excinfo:
        adapter.fetch("football", "h2h", {"entity_a": "A", "entity_b": "B"})
    assert "h2h/events" in excinfo.value.detail
    assert excinfo.value.source == "sofascore"


def test_fetch_bad_sport_or_category_raises_bad_request(adapter: SofascoreAdapter) -> None:
    with pytest.raises(BadRequest):
        adapter.fetch("handball", "live", {})
    with pytest.raises(BadRequest):
        adapter.fetch("football", "standings", {})


# --- lineups (live-probed 2026-09-20; recorded fixtures above) ----------------


def _lineups_source_response() -> SourceResponse:
    return SourceResponse(
        source="sofascore",
        payload={
            "team_a_page": json.loads((FIX / "football/lineups_team_next_a.json").read_text()),
            "team_b_page": json.loads((FIX / "football/lineups_team_next_b.json").read_text()),
            "selected_event": {
                "id": 16363879,
                "startTimestamp": 1791649800,
                "status": {"type": "notstarted"},
                "homeTeam": {"name": "Manchester United", "id": 33},
                "awayTeam": {"name": "Tottenham Hotspur", "id": 40},
                "homeScore": {},
                "awayScore": {},
                "tournament": {
                    "uniqueTournament": {"name": "Premier League"},
                    "category": {
                        "sport": {"slug": "football"},
                        "country": {"name": "England"},
                    },
                },
            },
            "lineups": json.loads((FIX / "football/lineups.json").read_text()),
        },
        url="https://api.sofascore.com/api/v1/event/16363879/lineups",
        status=200,
        fetched_at=FETCHED_AT,
    )


def test_parse_lineups_event_and_sheets(adapter: SofascoreAdapter) -> None:
    outcome = adapter.parse_lineups(_lineups_source_response())
    assert len(outcome.events) == 1
    ev = outcome.events[0]
    assert ev.home.name == "Manchester United"
    assert ev.away.name == "Tottenham Hotspur"
    assert ev.external.source == "sofascore"
    assert ev.external.source_event_id == "16363879"
    assert ev.competition is not None and ev.competition.name == "Premier League"
    doc = outcome.lineups
    assert doc is not None and doc["confirmed"] is False
    home, away = doc["home"], doc["away"]
    assert home["formation"] == "4-2-3-1"
    assert home["coach"] == "Ruben Amorim"
    assert home["players"] == [
        {"name": "Andre Onana", "position": "G", "jersey_number": 24, "substitute": False},
        {"name": "Antony", "position": "F", "jersey_number": 21, "substitute": True},
    ]
    assert home["missing_players"] == ["Mason Mount"]
    assert away["formation"] == "4-3-3"
    assert away["coach"] is None
    assert away["players"][0]["name"] == "Guglielmo Vicario"
    assert away["missing_players"] == []
    assert any("provisional" in w for w in outcome.warnings)


def test_parse_lineups_confirmed_document_has_no_provisional_warning(
    adapter: SofascoreAdapter,
) -> None:
    raw = json.loads((FIX / "football/lineups.json").read_text())
    raw["confirmed"] = True
    resp = _lineups_source_response()
    resp = SourceResponse(
        source=resp.source,
        payload={**resp.payload, "lineups": raw},  # type: ignore[arg-type]
        url=resp.url,
        status=resp.status,
        fetched_at=resp.fetched_at,
    )
    outcome = adapter.parse_lineups(resp)
    assert outcome.lineups is not None and outcome.lineups["confirmed"] is True
    assert not any("provisional" in w for w in outcome.warnings)


def test_parse_lineups_missing_sheets_raises_no_data(adapter: SofascoreAdapter) -> None:
    resp = SourceResponse(
        source="sofascore",
        payload={"lineups": {"foo": 1}},
        url="https://api.sofascore.com/api/v1/event/1/lineups",
        status=200,
        fetched_at=FETCHED_AT,
    )
    with pytest.raises(NoData):
        adapter.parse_lineups(resp)


def test_fetch_lineups_resolves_shared_event(
    adapter: SofascoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two search hits + two team pages + the lineups call (no network)."""
    search_a = {"results": [{"entity": {"id": 33, "name": "Manchester United", "type": 0, "sport": {"slug": "football"}}}]}
    search_b = {"results": [{"entity": {"id": 40, "name": "Tottenham Hotspur", "type": 0, "sport": {"slug": "football"}}}]}
    pages = {
        "/team/33/events/next/0": (FIX / "football/lineups_team_next_a.json").read_text(),
        "/team/40/events/next/0": (FIX / "football/lineups_team_next_b.json").read_text(),
    }
    lineups_bytes = (FIX / "football/lineups.json").read_text()
    calls: list[str] = []

    def fake_request(url: str) -> SourceResponse:
        calls.append(url)
        if "/search/all" in url:
            payload = search_b if "Tottenham" in url else search_a
        elif "/events/next/0" in url:
            payload = pages[url.split("www.sofascore.com/api/v1")[1]]
        else:
            payload = lineups_bytes
        return SourceResponse(
            source="sofascore", payload=payload, url=url, status=200, fetched_at=FETCHED_AT
        )

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("football", "lineups", {"team_a": "Manchester United", "team_b": "Tottenham"})
    assert resp.url == "https://api.sofascore.com/api/v1/event/16363879/lineups"
    assert len(calls) == 5  # search x2 + team pages x2 + lineups
    payload = resp.payload
    assert isinstance(payload, dict)
    assert payload["selected_event"]["id"] == 16363879


def test_fetch_lineups_no_shared_event_raises_not_found(
    adapter: SofascoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    search_a = {"results": [{"entity": {"id": 33, "name": "Manchester United", "type": 0, "sport": {"slug": "football"}}}]}
    search_b = {"results": [{"entity": {"id": 40, "name": "Tottenham Hotspur", "type": 0, "sport": {"slug": "football"}}}]}

    def fake_request(url: str) -> SourceResponse:
        if "/search/all" in url:
            payload = search_b if "Tottenham" in url else search_a
        else:
            payload = {"events": []}  # both pages empty -> no shared fixture
        return SourceResponse(
            source="sofascore", payload=json.dumps(payload), url=url, status=200, fetched_at=FETCHED_AT
        )

    monkeypatch.setattr(adapter, "_request", fake_request)
    with pytest.raises(NotFound):
        adapter.fetch("football", "lineups", {"team_a": "Manchester United", "team_b": "Tottenham"})


def test_fetch_lineups_non_football_raises_not_found(adapter: SofascoreAdapter) -> None:
    with pytest.raises(NotFound) as excinfo:
        adapter.fetch("basketball", "lineups", {"team_a": "A", "team_b": "B"})
    assert "football" in excinfo.value.detail


def test_fetch_lineups_missing_teams_raises_bad_request(adapter: SofascoreAdapter) -> None:
    with pytest.raises(BadRequest):
        adapter.fetch("football", "lineups", {"team_a": "Only One"})
