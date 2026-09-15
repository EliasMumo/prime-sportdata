"""Flashscore adapter offline tests: fixtures parse, typed errors, lazy imports.

No network in this file. Fixtures under tests/fixtures/flashscore/ were
recorded live on 2026-09-02 (sidecar: tests/fixtures/flashscore/SOURCES.md).
Synthetic bodies used below for error-mapping tests are constructed inline and
are labeled synthetic — no such response was observed live this session.
"""

import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from prime_sportdata.errors import (
    BadRequest,
    DependencyMissing,
    NoData,
    NotFound,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.sources import flashscore as fs
from prime_sportdata.sources.base import ParseOutcome, SourceResponse

FIX = Path(__file__).parent / "fixtures" / "flashscore"
FETCHED_AT = "2026-09-02T18:40:00Z"

URL_TODAY_FB = "https://2.flashscore.ninja/2/x/feed/f_1_0_3_en_2"
URL_YESTERDAY_FB = "https://2.flashscore.ninja/2/x/feed/f_1_-1_3_en_2"
URL_TOMORROW_FB = "https://2.flashscore.ninja/2/x/feed/f_1_1_3_en_2"
URL_TODAY_BB = "https://2.flashscore.ninja/2/x/feed/f_3_0_3_en_2"
URL_TODAY_TN = "https://2.flashscore.ninja/2/x/feed/f_2_0_3_en_2"


def load(name: str, url: str) -> SourceResponse:
    return SourceResponse(
        source="flashscore",
        payload=(FIX / name).read_bytes(),
        url=url,
        status=200,
        fetched_at=FETCHED_AT,
    )


@pytest.fixture()
def adapter() -> fs.FlashscoreAdapter:
    return fs.FlashscoreAdapter()


# --- fixture parsing ----------------------------------------------------------


def test_module_import_does_not_require_scrapling() -> None:
    # the optional research extra must never be a module-level dependency
    assert "scrapling" not in sys.modules


def test_parse_football_today_statuses_and_scores(adapter: fs.FlashscoreAdapter) -> None:
    outcome = adapter.parse_events(load("football/day_today.txt", URL_TODAY_FB))
    assert isinstance(outcome, ParseOutcome)
    assert len(outcome.events) == 4
    by_id = {ev.external.source_event_id: ev for ev in outcome.events}
    # scheduled: no score
    sched = by_id["pdmRhdwH"]
    assert sched.status == "scheduled"
    assert sched.home.name == "Atl. Tucuman 2"
    assert sched.away.name == "San Lorenzo 2"
    assert sched.home.score is None and sched.away.score is None
    assert sched.competition is not None and sched.competition.name == "Reserve League - Clausura"
    assert sched.competition.country == "Argentina"
    # live: current score 1-1, no live clock detail (feed carries none)
    live = by_id["feIxEokD"]
    assert live.status == "live"
    assert live.home.name == "Slavija"
    assert live.home.score == 1 and live.away.score == 1
    assert live.home.current_score is True
    assert live.live_detail is None
    assert live.score_lines == []
    assert live.competition is not None and live.competition.name == "Prva Liga - RS"
    assert live.competition.country == "Bosnia and Herzegovina"
    # postponed: AB=3 without AG + AC=4 marker
    postponed = by_id["CppsZsN5"]
    assert postponed.status == "postponed"
    assert postponed.home.name == "Doncaster"
    assert postponed.away.name == "Liverpool U21"
    assert postponed.home.score is None
    # finished Sassuolo 2-1: cross-source kickoff-verified match (13:00:00Z)
    finished = by_id["KxrZoe94"]
    assert finished.status == "finished"
    assert finished.home.name == "Sassuolo"
    assert finished.away.name == "Frosinone"
    assert finished.home.score == 2 and finished.away.score == 1
    assert finished.home.current_score is False
    assert finished.start_time_utc == "2026-09-02T13:00:00+00:00"
    assert finished.competition is not None and finished.competition.name == "Coppa Italia"
    assert finished.competition.country == "Italy"
    # football rows ship no period lines (BC/BD disproven as half-time)
    assert finished.score_lines == []
    # provenance stamped from the recorded response
    assert finished.external.source == "flashscore"
    assert finished.external.source_event_id == "KxrZoe94"
    assert finished.external.source_url == URL_TODAY_FB
    # standing caller-side warning present
    assert any("caller-side" in w for w in outcome.warnings)


def test_parse_football_yesterday_all_finished(adapter: fs.FlashscoreAdapter) -> None:
    outcome = adapter.parse_events(load("football/day_yesterday.txt", URL_YESTERDAY_FB))
    assert {e.status for e in outcome.events} == {"finished"}
    first = outcome.events[0]
    assert first.external.source_event_id == "pMYI1J6k"
    assert first.home.name == "Primeiro de Agosto"
    assert first.home.score == 2
    assert first.competition is not None and first.competition.country == "Angola"


def test_parse_football_tomorrow_all_scheduled(adapter: fs.FlashscoreAdapter) -> None:
    outcome = adapter.parse_events(load("football/day_tomorrow.txt", URL_TOMORROW_FB))
    assert {e.status for e in outcome.events} == {"scheduled"}
    assert all(e.home.score is None for e in outcome.events)
    first = outcome.events[0]
    assert first.home.name == "ES Setif"
    assert first.away.name == "Ben Aknoun"
    assert first.competition is not None
    assert first.competition.name == "Ligue 1"
    assert first.competition.country == "Algeria"
    assert first.start_time_utc is not None and first.start_time_utc.endswith("+00:00")


def test_parse_basketball_quarters_and_postponed(adapter: fs.FlashscoreAdapter) -> None:
    outcome = adapter.parse_events(load("basketball/day_today.txt", URL_TODAY_BB))
    assert len(outcome.events) == 10
    by_id = {ev.external.source_event_id: ev for ev in outcome.events}
    finished = by_id["CS2zfc8t"]  # Capiata Bulls 76-93 Dep. San Jose
    assert finished.status == "finished"
    assert finished.sport == "basketball"
    assert finished.home.name == "Capiata Bulls"
    assert finished.home.score == 76 and finished.away.score == 93
    assert [(l.period_label, l.home, l.away) for l in finished.score_lines] == [
        ("Q1", 21, 32),
        ("Q2", 22, 18),
        ("Q3", 27, 25),
        ("Q4", 6, 18),
    ]
    assert finished.competition is not None and finished.competition.name == "LNB - Clausura"
    assert finished.competition.country == "Paraguay"
    # every finished quarter sum must match the final score (verified on all 7)
    finished_rows = [e for e in outcome.events if e.status == "finished"]
    assert len(finished_rows) == 7
    for ev in finished_rows:
        assert len(ev.score_lines) == 4
        assert sum(l.home for l in ev.score_lines) == ev.home.score
        assert sum(l.away for l in ev.score_lines) == ev.away.score
    # scheduled + postponed rows carry no quarter lines or scores
    scheduled = by_id["YJAtVJk2"]
    assert scheduled.status == "scheduled" and scheduled.home.score is None
    postponed = by_id["ShIMg6yG"]
    assert postponed.status == "postponed" and postponed.home.score is None
    # "World" country sentinel -> None (club friendly)
    friendly = next(e for e in outcome.events if e.status == "finished" and e.competition is not None
                    and "Friendly" in e.competition.name)
    assert friendly.competition is not None and friendly.competition.country is None


def test_parse_tennis_sets_and_live_in_progress(adapter: fs.FlashscoreAdapter) -> None:
    outcome = adapter.parse_events(load("tennis/day_today.txt", URL_TODAY_TN))
    assert len(outcome.events) == 9
    by_id = {ev.external.source_event_id: ev for ev in outcome.events}
    # finished 5-setter: Cerundolo 2-3 Gea (AG/AH = sets won; S1..S5 = games)
    five = by_id["0GTuFzQh"]
    assert five.status == "finished"
    assert five.home.name == "Cerundolo J. M."
    assert five.away.name == "Gea A."
    assert five.home.score == 2 and five.away.score == 3
    assert [(l.period_label, l.home, l.away) for l in five.score_lines] == [
        ("S1", 3, 6),
        ("S2", 6, 2),
        ("S3", 6, 3),
        ("S4", 3, 6),
        ("S5", 1, 6),
    ]
    assert five.competition is not None and five.competition.name == "US Open (USA), hard"
    assert five.competition.country is None  # no ZY in the tennis league block
    # 3-set finish: lines stop where the set keys end (no invented sets)
    three = by_id["YP799xBN"]  # Fery 0-3 Musetti
    assert three.home.score == 0 and three.away.score == 3
    assert [(l.period_label, l.home, l.away) for l in three.score_lines] == [
        ("S1", 3, 6),
        ("S2", 3, 6),
        ("S3", 5, 7),
    ]
    # live tennis: sets-won score is current; the in-play set pair ships as an
    # S-line (Medvedev leading S1 4-1, sets 0-0)
    live = by_id["juHjCd3C"]
    assert live.status == "live"
    assert live.home.name == "Medvedev D."
    assert live.away.name == "Gorzny S."
    assert live.home.score == 0 and live.away.score == 0
    assert live.home.current_score is True
    assert [(l.period_label, l.home, l.away) for l in live.score_lines] == [("S1", 4, 1)]
    assert live.live_detail is None
    # scheduled tennis: no score, no lines
    sched = by_id["vNjKGgIj"]
    assert sched.status == "scheduled" and sched.home.score is None and sched.score_lines == []


def test_parse_all_fixture_events_are_stamped_and_typed(adapter: fs.FlashscoreAdapter) -> None:
    total = 0
    for name, url in [
        ("football/day_today.txt", URL_TODAY_FB),
        ("football/day_yesterday.txt", URL_YESTERDAY_FB),
        ("football/day_tomorrow.txt", URL_TOMORROW_FB),
        ("basketball/day_today.txt", URL_TODAY_BB),
        ("tennis/day_today.txt", URL_TODAY_TN),
    ]:
        for ev in adapter.parse_events(load(name, url)).events:
            total += 1
            assert ev.external.source == "flashscore"
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
            assert ev.start_time_utc is None or ev.start_time_utc.endswith("+00:00")
    assert total == 4 + 5 + 3 + 10 + 9


def test_parse_non_feed_payload_raises_no_data(adapter: fs.FlashscoreAdapter) -> None:
    # synthetic: a 1-byte '0' body was what the (malformed) probe URL returned
    resp = SourceResponse(
        source="flashscore", payload=b"0", url=URL_TODAY_FB, status=200, fetched_at=FETCHED_AT
    )
    with pytest.raises(NoData):
        adapter.parse_events(resp)
    html = SourceResponse(
        source="flashscore",
        payload=b"<html>not a feed</html>",
        url=URL_TODAY_FB,
        status=200,
        fetched_at=FETCHED_AT,
    )
    with pytest.raises(NoData):
        adapter.parse_events(html)


def test_parse_unknown_feed_sport_header_raises_no_data(adapter: fs.FlashscoreAdapter) -> None:
    resp = SourceResponse(
        source="flashscore", payload=b"SA\xc3\xb79\xc2\xac~AA\xc3\xb7x\xc2\xacAB\xc3\xb71\xc2\xac",
        url=URL_TODAY_FB, status=200, fetched_at=FETCHED_AT,
    )
    with pytest.raises(NoData):
        adapter.parse_events(resp)


# --- competition-name splitting edges ----------------------------------------


def test_competition_splitting() -> None:
    cases = [
        ({"ZA": "ENGLAND: EFL Trophy", "ZY": "England"}, "EFL Trophy", "England"),
        ({"ZA": "ATP - SINGLES: US Open (USA), hard"}, "US Open (USA), hard", None),
        ({"ZA": "WORLD: Club Friendly", "ZY": "World"}, "Club Friendly", None),
        ({"ZA": "ITALY: Coppa Italia", "ZY": "Italy"}, "Coppa Italia", "Italy"),
        ({}, None, None),
    ]
    for league, name, country in cases:
        comp = fs.FlashscoreAdapter._competition(league)
        if name is None:
            assert comp is None
        else:
            assert comp is not None
            assert comp.name == name
            assert comp.country == country


# --- fetch-layer typed-error mapping (synthetic bodies, offline) -------------


def outcome_for(status: int, body: bytes = b"SA\xc3\xb71\xc2\xac") -> SourceResponse:
    return fs._map_http_outcome("https://2.flashscore.ninja/2/x/feed/f_1_0_3_en_2", status, body, FETCHED_AT, "flashscore")


def test_map_403_to_source_blocked() -> None:
    with pytest.raises(SourceBlocked) as excinfo:
        outcome_for(403)
    assert excinfo.value.source == "flashscore"
    assert excinfo.value.detail.startswith("HTTP 403")


def test_map_429_to_rate_limited() -> None:
    with pytest.raises(RateLimited) as excinfo:
        outcome_for(429)
    assert excinfo.value.code == "rate_limited"


def test_map_404_to_not_found() -> None:
    with pytest.raises(NotFound) as excinfo:
        outcome_for(404)
    assert excinfo.value.detail.startswith("HTTP 404")


def test_map_400_to_bad_request() -> None:
    with pytest.raises(BadRequest):
        outcome_for(400)


def test_map_5xx_to_source_unavailable() -> None:
    with pytest.raises(SourceUnavailable) as excinfo:
        outcome_for(503)
    assert excinfo.value.detail.startswith("HTTP 503")


def test_map_200_html_to_source_blocked() -> None:
    # synthetic challenge body: no HTML challenge was observed live this session
    html = b"<!doctype html><html><body>checking your browser</body></html>"
    with pytest.raises(SourceBlocked):
        outcome_for(200, html)


def test_map_200_text_to_source_response() -> None:
    resp = outcome_for(200, b"SA\xc3\xb71\xc2\xac")
    assert isinstance(resp, SourceResponse)
    assert resp.status == 200
    assert resp.url.endswith("f_1_0_3_en_2")


# --- day-offset computation (offline, fixed Prague date) ---------------------


def test_day_offset_math_and_fetch_routing(adapter: fs.FlashscoreAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "_prague_today", lambda: date(2026, 9, 2))
    requested: list[str] = []

    def fake_request(url: str) -> SourceResponse:
        requested.append(url)
        return SourceResponse(
            source="flashscore", payload=b"SA\xc3\xb71\xc2\xac", url=url, status=200,
            fetched_at=FETCHED_AT,
        )

    monkeypatch.setattr(adapter, "_request", fake_request)
    adapter.fetch("football", "fixtures", {"date": "2026-09-02"})
    adapter.fetch("football", "fixtures", {})  # default: today
    adapter.fetch("football", "results", {"date": "2026-09-01"})
    adapter.fetch("football", "results", {})  # default: yesterday
    adapter.fetch("football", "live", {})
    adapter.fetch("football", "fixtures", {"date": "2026-09-03"})
    adapter.fetch("tennis", "fixtures", {"date": "2026-09-02"})
    adapter.fetch("basketball", "results", {"date": "2026-09-01"})
    assert requested == [
        "https://2.flashscore.ninja/2/x/feed/f_1_0_3_en_2",
        "https://2.flashscore.ninja/2/x/feed/f_1_0_3_en_2",
        "https://2.flashscore.ninja/2/x/feed/f_1_-1_3_en_2",
        "https://2.flashscore.ninja/2/x/feed/f_1_-1_3_en_2",
        "https://2.flashscore.ninja/2/x/feed/f_1_0_3_en_2",
        "https://2.flashscore.ninja/2/x/feed/f_1_1_3_en_2",
        "https://2.flashscore.ninja/2/x/feed/f_2_0_3_en_2",
        "https://2.flashscore.ninja/2/x/feed/f_3_-1_3_en_2",
    ]


def test_fetch_unverified_offsets_raise_not_found_with_evidence(
    adapter: fs.FlashscoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "_prague_today", lambda: date(2026, 9, 2))
    for future_date in ("2026-09-04", "2026-09-10"):
        with pytest.raises(NotFound) as excinfo:
            adapter.fetch("football", "fixtures", {"date": future_date})
        assert "-7" in excinfo.value.detail  # verified range evidence quoted
    with pytest.raises(NotFound):
        adapter.fetch("football", "results", {"date": "2026-08-01"})


def test_fetch_malformed_date_raises_bad_request(
    adapter: fs.FlashscoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "_prague_today", lambda: date(2026, 9, 2))
    with pytest.raises(BadRequest):
        adapter.fetch("football", "fixtures", {"date": "09/02/2026"})


def test_fetch_bad_sport_or_category_raises_bad_request(adapter: fs.FlashscoreAdapter) -> None:
    with pytest.raises(BadRequest):
        adapter.fetch("handball", "live", {})
    with pytest.raises(BadRequest):
        adapter.fetch("football", "standings", {})


def test_fetch_h2h_raises_not_found_with_evidence(adapter: fs.FlashscoreAdapter) -> None:
    with pytest.raises(NotFound) as excinfo:
        adapter.fetch("football", "h2h", {"entity_a": "Sassuolo", "entity_b": "Frosinone"})
    detail = excinfo.value.detail
    assert "df_hh" in detail
    assert "KxrZoe94" in detail
    assert excinfo.value.source == "flashscore"


# --- lazy scrapling import: DependencyMissing, never ImportError at import --


def test_escalate_without_scrapling_raises_dependency_missing(
    adapter: fs.FlashscoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "scrapling" not in sys.modules  # fixture environment: extra absent

    def fake_import(name: str, *args: Any, **kwargs: Any) -> ModuleType:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(DependencyMissing) as excinfo:
        adapter._escalate_to_scrapling("https://www.flashscore.com/football/")
    assert excinfo.value.code == "dependency_missing"
    assert "research" in excinfo.value.detail
    assert excinfo.value.source == "flashscore"


def test_escalate_with_scrapling_present_is_still_inactive(
    adapter: fs.FlashscoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType("scrapling")
    sys.modules["scrapling"] = module
    try:
        with pytest.raises(SourceUnavailable) as excinfo:
            adapter._escalate_to_scrapling("https://www.flashscore.com/football/")
        assert "not wired into fetch()" in excinfo.value.detail
    finally:
        del sys.modules["scrapling"]
