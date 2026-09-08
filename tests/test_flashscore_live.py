"""Live-network smoke tests for the flashscore adapter (opt-in: -m live).

These execute the adapter's real network path against the 2026-09-02-verified
x-fsign feed host (2.flashscore.ninja, one static UA + retry discipline).
Sequential, >= 1.3 s between hits, tiny volume (SPEC Ban-risk policy). Outcome
of this file's run is recorded in docs/source-health.md.
"""

import time
from datetime import timedelta

import pytest

from prime_sportdata.errors import BadRequest, NotFound
from prime_sportdata.sources import flashscore as fs
from prime_sportdata.sources.base import ParseOutcome

_MARK = pytest.mark.live
_SPACING_S = 1.3


def _spaced() -> None:
    time.sleep(_SPACING_S)


def _iso(days_offset: int) -> str:
    return (fs._prague_today() + timedelta(days=days_offset)).isoformat()


@_MARK
def test_live_football_fixtures_today_roundtrip() -> None:
    _spaced()
    adapter = fs.FlashscoreAdapter()
    resp = adapter.fetch("football", "fixtures", {"date": _iso(0)})
    assert resp.status == 200
    assert resp.url.endswith("f_1_0_3_en_2")
    outcome = adapter.parse_events(resp)
    assert isinstance(outcome, ParseOutcome)
    assert len(outcome.events) > 0
    assert all(ev.external.source == "flashscore" for ev in outcome.events)
    assert all(ev.sport == "football" for ev in outcome.events)
    assert all(ev.status in ("scheduled", "live", "finished", "postponed") for ev in outcome.events)
    print(f"football today events parsed: {len(outcome.events)}")


@_MARK
def test_live_football_results_yesterday_roundtrip() -> None:
    _spaced()
    adapter = fs.FlashscoreAdapter()
    resp = adapter.fetch("football", "results", {"date": _iso(-1)})
    assert resp.status == 200
    assert resp.url.endswith("f_1_-1_3_en_2")
    outcome = adapter.parse_events(resp)
    assert isinstance(outcome, ParseOutcome)
    assert len(outcome.events) > 0
    print(f"football yesterday events parsed: {len(outcome.events)}")


@_MARK
def test_live_basketball_today_roundtrip() -> None:
    _spaced()
    adapter = fs.FlashscoreAdapter()
    resp = adapter.fetch("basketball", "fixtures", {"date": _iso(0)})
    assert resp.status == 200
    outcome = adapter.parse_events(resp)
    assert isinstance(outcome, ParseOutcome)
    assert all(ev.sport == "basketball" for ev in outcome.events)
    print(f"basketball today events parsed: {len(outcome.events)}")


@_MARK
def test_live_typed_errors_never_reach_the_network() -> None:
    _spaced()
    adapter = fs.FlashscoreAdapter()
    # no verified team->event resolution -> h2h is a pre-network typed error
    with pytest.raises(NotFound):
        adapter.fetch("football", "h2h", {"entity_a": "A", "entity_b": "B"})
    # unverified day offsets likewise raise before any request
    with pytest.raises(NotFound):
        adapter.fetch("football", "results", {"date": _iso(-3)})
    with pytest.raises(BadRequest):
        adapter.fetch("football", "fixtures", {"date": "not-a-date"})
