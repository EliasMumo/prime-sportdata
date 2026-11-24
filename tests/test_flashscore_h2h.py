"""Flashscore h2h (``df_hh_1_<eventId>``) offline regression tests.

The recorded body under ``tests/fixtures/flashscore/h2h/`` is the 2026-09-02
head slice of the real per-event feed (provenance in
``tests/fixtures/flashscore/SOURCES.md``): the target event row (``KP`` =
KxrZoe94, Sassuolo 2:1 Frosinone, kickoff 2026-09-02T13:00:00Z) plus seven of
Sassuolo's own recent matches.  It contains **no** previous Sassuolo-Frosinone
meeting, so the recorded body proves corroboration and the "same pair only"
filter (valid empty), while the numbered synthetic bodies below — built with
the exact recorded key set (KC/KP/KF/KJ/KK/KL/KU/KT/KS/KB) — cover the meeting
list, orientation, duplicate and boundary rules.  No network in this file.
"""

from datetime import datetime
from pathlib import Path

import pytest

from prime_sportdata.errors import BadRequest, NoData, NotFound
from prime_sportdata.sources import flashscore as fs
from prime_sportdata.sources.base import SourceResponse

FIX = Path(__file__).parent / "fixtures" / "flashscore"
FETCHED_AT = "2026-09-02T18:40:00Z"
H2H_URL = "https://2.flashscore.ninja/2/x/feed/df_hh_1_KxrZoe94"

TARGET_EVENT = "KxrZoe94"
# 1788354000 == 2026-09-02T13:00:00Z (cross-checked in SOURCES.md).
KICKOFF_EPOCH = 1788354000
KICKOFF = "2026-09-02T13:00:00Z"


def _params(**overrides: str) -> dict[str, str]:
    base = {
        "entity_a": "Sassuolo",
        "entity_b": "Frosinone",
        "event": TARGET_EVENT,
        "kickoff": KICKOFF,
    }
    base.update(overrides)
    return base


def _resp(body: str, url: str = H2H_URL) -> SourceResponse:
    return SourceResponse(
        source="flashscore",
        payload=body.encode("utf-8"),
        url=url,
        status=200,
        fetched_at=FETCHED_AT,
    )


def _recorded() -> SourceResponse:
    return _resp((FIX / "h2h" / "df_hh_1_KxrZoe94.txt").read_text(encoding="utf-8"))


def _row(
    *,
    event: str,
    kickoff: int | str,
    home: str,
    away: str,
    home_score: int | None = None,
    away_score: int | None = None,
    competition: str = "Serie A",
    country: str = "Italy",
    venue: str | None = "home",
    score_text: str | None = None,
) -> str:
    """One meeting row in the exact recorded field order (synthetic values)."""
    fields = [
        f"KC÷{kickoff}",
        f"KP÷{event}",
        f"KF÷{competition}",
        "AC÷3",
        "KG÷98",
        f"KH÷{country}",
        "KI÷SA",
        f"KJ÷{home}",
        "UE÷home-slug",
        f"FH÷{home}",
        "FJ÷",
        f"KK÷{away}",
        "UG÷away-slug",
        f"FK÷{away}",
        "FL÷",
    ]
    if score_text is not None:
        fields.append(f"KL÷{score_text}")
    elif home_score is not None and away_score is not None:
        fields.append(f"KL÷{home_score}:{away_score}")
    if home_score is not None:
        fields.append(f"KU÷{home_score}")
    if away_score is not None:
        fields.append(f"KT÷{away_score}")
    if venue is not None:
        fields.append(f"KS÷{venue}")
    return "¬".join(fields) + "¬"


def _feed(rows: list[str], *, anchor: str = "Sassuolo") -> str:
    return (
        "SA÷1¬~"
        "KA÷Overall¬IS÷¬~"
        f"KB÷Last matches: {anchor}¬~" + "~".join(rows) + "~"
    )


def _target_row(**overrides: str | int | None) -> str:
    values: dict[str, object] = {
        "event": TARGET_EVENT,
        "kickoff": KICKOFF_EPOCH,
        "home": "Sassuolo",
        "away": "Frosinone",
        "home_score": 2,
        "away_score": 1,
        "venue": "home",
    }
    values.update(overrides)
    return _row(**values)  # type: ignore[arg-type]


@pytest.fixture()
def adapter() -> fs.FlashscoreAdapter:
    return fs.FlashscoreAdapter()


# --- fetch: verified event id, kickoff and sport gate --------------------------


def test_fetch_h2h_requests_the_verified_event_feed(
    adapter: fs.FlashscoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []

    def fake_request(url: str) -> SourceResponse:
        requested.append(url)
        return _recorded()

    monkeypatch.setattr(adapter, "_request", fake_request)
    resp = adapter.fetch("football", "h2h", _params())
    assert requested == ["https://2.flashscore.ninja/2/x/feed/df_hh_1_KxrZoe94"]
    assert resp.status == 200


def test_fetch_h2h_without_event_keeps_the_name_only_refusal(
    adapter: fs.FlashscoreAdapter,
) -> None:
    with pytest.raises(NotFound) as excinfo:
        adapter.fetch("football", "h2h", {"entity_a": "Sassuolo", "entity_b": "Frosinone"})
    assert "df_hh" in excinfo.value.detail
    assert excinfo.value.source == "flashscore"


def test_fetch_h2h_without_kickoff_is_an_honest_empty_without_a_request(
    adapter: fs.FlashscoreAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(url: str) -> SourceResponse:
        raise AssertionError("a request must not be sent without a kickoff")

    monkeypatch.setattr(adapter, "_request", boom)
    with pytest.raises(NoData) as excinfo:
        adapter.fetch(
            "football",
            "h2h",
            {"entity_a": "Sassuolo", "entity_b": "Frosinone", "event": TARGET_EVENT},
        )
    assert "kickoff" in excinfo.value.detail


def test_fetch_h2h_rejects_a_malformed_or_naive_kickoff(
    adapter: fs.FlashscoreAdapter,
) -> None:
    with pytest.raises(BadRequest, match="ISO-8601"):
        adapter.fetch("football", "h2h", _params(kickoff="02/09/2026 13:00"))
    with pytest.raises(BadRequest, match="UTC offset"):
        adapter.fetch("football", "h2h", _params(kickoff="2026-09-02T13:00:00"))


def test_fetch_h2h_is_football_only(adapter: fs.FlashscoreAdapter) -> None:
    with pytest.raises(NotFound, match="probe-verified only for football"):
        adapter.fetch("tennis", "h2h", _params())


# --- parse: recorded fixture --------------------------------------------------


def test_parse_h2h_recorded_feed_corroborates_the_event_and_ships_no_other_pair(
    adapter: fs.FlashscoreAdapter,
) -> None:
    outcome = adapter.parse_h2h(_recorded(), params=_params())
    # The recorded slice lists the target event plus Sassuolo's own recent
    # matches — none of them is a previous Sassuolo-Frosinone meeting.
    assert outcome.events == []
    assert any("no completed meeting" in warning for warning in outcome.warnings)
    assert all("source=flashscore" in warning for warning in outcome.warnings)


def test_parse_h2h_full_recorded_feed_returns_real_prior_meetings(
    adapter: fs.FlashscoreAdapter,
) -> None:
    """Full authorized capture (2026-11-24) of the real per-event feed.

    The body repeats its sections (Last matches: Sassuolo / Last matches:
    Frosinone / Head-to-head) seven times, so it also proves the dedupe and the
    kickoff boundary against real data: the target event and Sassuolo's later
    matches must not leak into the meeting list.
    """
    resp = _resp((FIX / "h2h" / "df_hh_1_KxrZoe94.full.txt").read_text(encoding="utf-8"))
    outcome = adapter.parse_h2h(resp, params=_params())

    events = outcome.events
    assert len(events) == 14
    ids = [event.external.source_event_id for event in events]
    assert len(ids) == len(set(ids)), "repeated feed sections must not duplicate a meeting"
    assert TARGET_EVENT not in ids, "the target event itself is not a prior meeting"
    assert all(
        int(datetime.fromisoformat(event.start_time_utc).timestamp()) < KICKOFF_EPOCH
        for event in events
    )
    assert all(event.status == "finished" for event in events)
    assert all(event.external.source == "flashscore" for event in events)

    by_id = {event.external.source_event_id: event for event in events}
    # Orientation and scores stay with the meeting's own home/away columns.
    latest = by_id["dK7ORdPG"]
    assert (latest.home.name, latest.home.score) == ("Sassuolo", 0)
    assert (latest.away.name, latest.away.score) == ("Frosinone", 1)
    assert latest.competition is not None and latest.competition.name == "Serie B"
    reverse = by_id["jF6iQcvq"]
    assert (reverse.home.name, reverse.home.score) == ("Frosinone", 1)
    assert (reverse.away.name, reverse.away.score) == ("Sassuolo", 2)
    oldest = by_id["GUiGnEUr"]
    assert oldest.start_time_utc == "2008-12-06T15:00:00+00:00"
    assert (oldest.home.name, oldest.home.score) == ("Frosinone", 2)
    assert (oldest.away.name, oldest.away.score) == ("Sassuolo", 2)


# --- parse: pair meetings, orientation, dedupe and kickoff boundary -----------


def test_parse_h2h_returns_completed_pair_meetings_strictly_before_kickoff(
    adapter: fs.FlashscoreAdapter,
) -> None:
    older = _row(
        event="PriorMeeting", kickoff=1788021000, home="Sassuolo", away="Frosinone",
        home_score=2, away_score=1, venue="home",
    )
    # Reversed home/away orientation is legitimate for a previous meeting and
    # must keep each score aligned with its own team.
    reversed_leg = _row(
        event="PriorMeeting2", kickoff=1787510700, home="Frosinone", away="Sassuolo",
        home_score=1, away_score=0, venue="away",
    )
    duplicate = older
    at_kickoff = _row(
        event="SameInstant", kickoff=KICKOFF_EPOCH, home="Sassuolo", away="Frosinone",
        home_score=0, away_score=0, venue="home",
    )
    after_kickoff = _row(
        event="LaterMeeting", kickoff=KICKOFF_EPOCH + 86_400, home="Sassuolo",
        away="Frosinone", home_score=3, away_score=3, venue="home",
    )
    other_opponent = _row(
        event="OtherPair", kickoff=1786984200, home="Sassuolo", away="Torino",
        home_score=3, away_score=0, venue="home",
    )
    body = _feed([_target_row(), older, reversed_leg, duplicate, at_kickoff, after_kickoff, other_opponent])

    outcome = adapter.parse_h2h(_resp(body), params=_params())

    assert [event.external.source_event_id for event in outcome.events] == [
        "PriorMeeting",
        "PriorMeeting2",
    ]
    first, second = outcome.events
    assert (first.home.name, first.home.score) == ("Sassuolo", 2)
    assert (first.away.name, first.away.score) == ("Frosinone", 1)
    assert first.status == "finished"
    assert first.start_time_utc == "2026-08-29T16:30:00+00:00"
    assert first.competition is not None and first.competition.name == "Serie A"
    assert first.competition.country == "Italy"
    # Reversed historical leg: home/away and scores stay with their teams.
    assert (second.home.name, second.home.score) == ("Frosinone", 1)
    assert (second.away.name, second.away.score) == ("Sassuolo", 0)
    assert all(event.external.source == "flashscore" for event in outcome.events)


def test_parse_h2h_rejects_a_reversed_target_orientation(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([_target_row()])
    with pytest.raises(NoData) as excinfo:
        adapter.parse_h2h(_resp(body), params=_params(entity_a="Frosinone", entity_b="Sassuolo"))
    assert "reversed" in excinfo.value.detail
    assert "orientation" in excinfo.value.detail


def test_parse_h2h_rejects_mismatched_participants(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([_target_row()])
    with pytest.raises(NoData, match="does not match the requested participants"):
        adapter.parse_h2h(_resp(body), params=_params(entity_b="Torino"))


def test_parse_h2h_rejects_an_ambiguous_target_row(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([
        _target_row(),
        _target_row(home="Frosinone", away="Sassuolo", venue="home"),
    ])
    with pytest.raises(NoData, match="conflicting rows for event"):
        adapter.parse_h2h(_resp(body), params=_params())


def test_parse_h2h_rejects_a_missing_target_row(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([
        _row(
            event="SomeOtherMeeting", kickoff=1788021000, home="Sassuolo",
            away="Frosinone", home_score=2, away_score=1, venue="home",
        ),
    ])
    with pytest.raises(NoData, match="does not contain the verified event"):
        adapter.parse_h2h(_resp(body), params=_params())


def test_parse_h2h_rejects_a_target_kickoff_mismatch(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([_target_row()])
    with pytest.raises(NoData, match="does not match the verified scheduled kickoff"):
        adapter.parse_h2h(
            _resp(body),
            params=_params(kickoff="2026-09-02T15:00:00Z"),
        )


def test_parse_h2h_rejects_same_name_participants(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([_target_row()])
    with pytest.raises(NoData, match="cannot be told apart"):
        adapter.parse_h2h(_resp(body), params=_params(entity_a="Sassuolo", entity_b="Sassuolo"))


def test_parse_h2h_drops_an_inconsistent_duplicate_of_the_same_event(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([
        _target_row(),
        _row(
            event="PriorMeeting", kickoff=1788021000, home="Sassuolo",
            away="Frosinone", home_score=2, away_score=1, venue="home",
        ),
        _row(
            event="PriorMeeting", kickoff=1788021000, home="Sassuolo",
            away="Frosinone", home_score=4, away_score=0, venue="home",
        ),
    ])
    with pytest.raises(NoData, match="conflicting"):
        adapter.parse_h2h(_resp(body), params=_params())


def test_parse_h2h_upcoming_event_is_corroborated_from_section_anchors(
    adapter: fs.FlashscoreAdapter,
) -> None:
    """Upcoming fixtures have no target row; the feed's anchors prove the pair.

    Recorded 2026-11-24 (df_hh_1_dbLeaGLk): an upcoming fixture's feed carries
    prior meetings but never its own event row.
    """
    prior = _row(
        event="PriorMeeting", kickoff=1788021000, home="Sassuolo",
        away="Frosinone", home_score=2, away_score=1, venue="home",
    )
    reverse = _row(
        event="PriorMeeting2", kickoff=1787510700, home="Frosinone",
        away="Sassuolo", home_score=1, away_score=0, venue="home",
    )
    at_or_after = _row(
        event="LaterMeeting", kickoff=KICKOFF_EPOCH + 86_400, home="Sassuolo",
        away="Frosinone", home_score=3, away_score=3, venue="home",
    )
    body = (
        "SA÷1¬~KA÷Overall¬IS÷¬~"
        "KB÷Last matches: Sassuolo¬~" + prior + "~" + at_or_after + "~"
        "KB÷Last matches: Frosinone¬~" + reverse + "~"
        "KB÷Head-to-head matches¬~" + prior + "~"
    )
    outcome = adapter.parse_h2h(_resp(body), params=_params())

    assert [event.external.source_event_id for event in outcome.events] == [
        "PriorMeeting",
        "PriorMeeting2",
    ]
    assert outcome.events[0].home.name == "Sassuolo"
    assert outcome.events[1].home.name == "Frosinone"
    assert any("does not contain the target event row" in warning for warning in outcome.warnings)


def test_parse_h2h_upcoming_event_matches_country_suffixed_names(
    adapter: fs.FlashscoreAdapter,
) -> None:
    """The day feed tags clubs "(Kor)"; the h2h feed anchors bare names."""
    prior = _row(
        event="PriorMeeting", kickoff=1788021000, home="Township Rollers",
        away="Prisons XI Gaborone", home_score=1, away_score=0, venue="home",
    )
    reverse = _row(
        event="PriorMeeting2", kickoff=1787510700, home="Prisons XI Gaborone",
        away="Township Rollers", home_score=0, away_score=1, venue="home",
    )
    body = (
        "SA÷1¬~KB÷Last matches: Township Rollers¬~" + prior + "~"
        "KB÷Last matches: Prisons XI Gaborone¬~" + reverse + "~"
    )
    outcome = adapter.parse_h2h(
        _resp(body),
        params=_params(
            entity_a="Township Rollers (Bot)",
            entity_b="Prisons XI Gaborone (Bot)",
        ),
    )
    assert [event.external.source_event_id for event in outcome.events] == [
        "PriorMeeting",
        "PriorMeeting2",
    ]
    assert outcome.events[0].home.name == "Township Rollers"


def test_parse_h2h_upcoming_event_with_foreign_anchors_is_rejected(
    adapter: fs.FlashscoreAdapter,
) -> None:
    row = _row(
        event="PriorMeeting", kickoff=1788021000, home="Sassuolo",
        away="Frosinone", home_score=2, away_score=1, venue="home",
    )
    body = (
        "SA÷1¬~KB÷Last matches: Torino¬~" + row + "~"
        "KB÷Last matches: Cesena¬~"
    )
    with pytest.raises(NoData, match="do not anchor on both requested participants"):
        adapter.parse_h2h(_resp(body), params=_params())


# --- parse: unavailable data --------------------------------------------------


def test_parse_h2h_rejects_a_malformed_payload(adapter: fs.FlashscoreAdapter) -> None:
    with pytest.raises(NoData, match="not a flashscore h2h feed"):
        adapter.parse_h2h(_resp("not-a-feed"), params=_params())


def test_parse_h2h_rejects_a_pair_row_without_an_event_id(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([
        _target_row(),
        _row(
            event="", kickoff=1788021000, home="Sassuolo", away="Frosinone",
            home_score=2, away_score=1, venue="home",
        ),
    ])
    with pytest.raises(NoData, match="no source event id"):
        adapter.parse_h2h(_resp(body), params=_params())


def test_parse_h2h_rejects_an_unusable_meeting_timestamp(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([
        _target_row(),
        _row(
            event="PriorMeeting", kickoff="unknown", home="Sassuolo",
            away="Frosinone", home_score=2, away_score=1, venue="home",
        ),
    ])
    with pytest.raises(NoData, match="no usable timestamp"):
        adapter.parse_h2h(_resp(body), params=_params())


def test_parse_h2h_rejects_a_meeting_without_a_completed_score(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([
        _target_row(),
        _row(
            event="PriorMeeting", kickoff=1788021000, home="Sassuolo",
            away="Frosinone", venue="home",
        ),
    ])
    with pytest.raises(NoData, match="no completed score"):
        adapter.parse_h2h(_resp(body), params=_params())


def test_parse_h2h_rejects_disagreeing_score_fields(
    adapter: fs.FlashscoreAdapter,
) -> None:
    body = _feed([
        _target_row(),
        _row(
            event="PriorMeeting", kickoff=1788021000, home="Sassuolo",
            away="Frosinone", home_score=2, away_score=1, venue="home",
            score_text="1:2",
        ),
    ])
    with pytest.raises(NoData, match="score fields disagree"):
        adapter.parse_h2h(_resp(body), params=_params())


def test_parse_h2h_rejects_a_contradicting_section_anchor(
    adapter: fs.FlashscoreAdapter,
) -> None:
    # KS says the anchored team was away, but the anchored team is KJ (home).
    body = _feed([
        _target_row(),
        _row(
            event="PriorMeeting", kickoff=1788021000, home="Sassuolo",
            away="Frosinone", home_score=2, away_score=1, venue="away",
        ),
    ])
    with pytest.raises(NoData, match="orientation is ambiguous"):
        adapter.parse_h2h(_resp(body), params=_params())
