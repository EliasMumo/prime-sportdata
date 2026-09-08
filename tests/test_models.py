"""Normalized model tests: validation, envelope shapes (SPEC "Envelope")."""

import pytest
from pydantic import ValidationError

from prime_sportdata.models import (
    Envelope,
    ErrorBody,
    Event,
    EventsPayload,
    H2HPayload,
    ScoreLine,
)

ALL_CATEGORIES = ["fixtures", "live", "results", "h2h"]


def make_event(**overrides):
    base = {
        "sport": "football",
        "external": {
            "source": "flashscore",
            "source_event_id": "abc123",
            "source_url": "https://www.flashscore.com/football/match/abc123/",
        },
        "start_time_utc": "2026-09-02T18:30:00Z",
        "status": "scheduled",
        "home": {"name": "Team A", "source_id": "t1", "score": None, "current_score": False},
        "away": {"name": "Team B", "source_id": "t2", "score": None, "current_score": False},
    }
    base.update(overrides)
    return base


def make_meta(**overrides):
    base = {
        "sport": "football",
        "category": "fixtures",
        "source": "flashscore",
        "cached": False,
        "fetched_at_utc": "2026-09-02T17:00:00Z",
        "latency_ms": 123,
        "request": {"date": "2026-09-02", "league": None, "limit": 50},
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_event_full_round_trip():
    event = Event.model_validate(make_event())
    assert event.sport == "football"
    assert event.status == "scheduled"
    assert event.home.name == "Team A"
    assert event.external.source_event_id == "abc123"
    assert event.score_lines == []
    assert event.competition is None
    assert event.live_detail is None
    assert event.round_label is None
    assert Event.model_validate(event.model_dump()) == event


def test_event_defaults_are_nullable():
    event = Event.model_validate(
        make_event(start_time_utc=None, home={"name": "A"}, away={"name": "B"})
    )
    assert event.start_time_utc is None
    assert event.home.source_id is None
    assert event.home.score is None
    assert event.home.current_score is False


def test_event_team_source_id_allows_int_or_str():
    event = Event.model_validate(
        make_event(home={"name": "A", "source_id": 42}, away={"name": "B", "source_id": "x9"})
    )
    assert event.home.source_id == 42
    assert event.away.source_id == "x9"


def test_event_rejects_unknown_sport_and_status():
    with pytest.raises(ValidationError):
        Event.model_validate(make_event(sport="cricket"))
    with pytest.raises(ValidationError):
        Event.model_validate(make_event(status="delayed"))


def test_event_requires_external_and_competition_optional():
    bad = make_event()
    del bad["external"]
    with pytest.raises(ValidationError):
        Event.model_validate(bad)
    event = Event.model_validate(
        make_event(competition={"name": "Premier League"})
    )
    assert event.competition.country is None
    event = Event.model_validate(
        make_event(competition={"name": "Premier League", "country": "England"})
    )
    assert event.competition.country == "England"


def test_tennis_event_with_sets_and_live_detail():
    payload = make_event(
        sport="tennis",
        status="live",
        live_detail="2nd set",
        round_label="Semifinal",
        score_lines=[
            {"period_label": "S1", "home": 6, "away": 4},
            {"period_label": "S2", "home": 3, "away": 6},
        ],
    )
    event = Event.model_validate(payload)
    assert [s.period_label for s in event.score_lines] == ["S1", "S2"]
    assert event.live_detail == "2nd set"
    assert event.round_label == "Semifinal"


def test_score_line_period_label_is_free_str_with_conventions():
    for label in ["H1", "H2", "FT", "Q1", "Q4", "OT", "S1", "2nd set"]:
        assert ScoreLine(period_label=label, home=1, away=2).period_label == label


def test_events_payload_envelope_shapes():
    env = Envelope.model_validate(
        {"data": {"events": [make_event()]}, "meta": make_meta()}
    )
    assert isinstance(env.data, EventsPayload)
    assert len(env.data.events) == 1
    assert env.meta.warnings == []
    assert env.meta.request.limit == 50


def test_h2h_payload_with_summaries():
    env = Envelope.model_validate(
        {
            "data": {
                "events": [],
                "home": {"name": "Team A", "summary": "W3 D1 L2 in last 6 vs Team B"},
                "away": {"name": "Team B"},
            },
            "meta": make_meta(category="h2h"),
        }
    )
    assert isinstance(env.data, H2HPayload)
    assert env.data.home.summary == "W3 D1 L2 in last 6 vs Team B"
    assert env.data.away.summary is None  # omitted when not derivable


def test_h2h_payload_missing_side_rejected():
    with pytest.raises(ValidationError):
        Envelope.model_validate(
            {
                "data": {"events": [], "home": {"name": "Team A"}},
                "meta": make_meta(category="h2h"),
            }
        )


def test_events_payload_rejects_h2h_extra_keys():
    with pytest.raises(ValidationError):
        EventsPayload.model_validate({"events": [], "home": {"name": "A"}, "away": {"name": "B"}})


def test_meta_enforces_literals_and_warnings_default():
    with pytest.raises(ValidationError):
        Envelope.model_validate(
            {"data": {"events": []}, "meta": make_meta(category="standings")}
        )
    env = Envelope.model_validate(
        {"data": {"events": []}, "meta": make_meta(sport="tennis")}
    )
    assert env.meta.sport == "tennis"


def test_error_body_shape():
    body = ErrorBody.model_validate(
        {
            "error": {
                "code": "source_unavailable",
                "detail": "TCP timeout",
                "source": "livescore",
                "tried_sources": ["flashscore", "sofascore", "livescore"],
            }
        }
    )
    assert body.error.code == "source_unavailable"
    assert body.error.tried_sources == ["flashscore", "sofascore", "livescore"]
    minimal = ErrorBody.model_validate({"error": {"code": "no_data", "detail": "empty"}})
    assert minimal.error.source is None
    assert minimal.error.tried_sources == []
