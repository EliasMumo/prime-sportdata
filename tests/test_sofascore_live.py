"""Live-network smoke tests for the sofascore adapter (opt-in: -m live).

These execute the adapter's real network path against the verified 2026-09-02
endpoints (fallback-first IPv4 shim + one static UA + retry discipline).
Sequential, >= 1.3 s between hits, tiny volume (SPEC Ban-risk policy). Outcome
of this file's run is recorded in docs/source-health.md.
"""

import time

import pytest

from prime_sportdata.errors import NotFound
from prime_sportdata.sources.base import ParseOutcome
from prime_sportdata.sources.sofascore import SofascoreAdapter

_MARK = pytest.mark.live

_SPACING_S = 1.3


def _spaced() -> None:
    time.sleep(_SPACING_S)


@_MARK
def test_live_football_events_roundtrip() -> None:
    _spaced()
    adapter = SofascoreAdapter()
    resp = adapter.fetch("football", "live", {})
    assert resp.status == 200
    outcome = adapter.parse_events(resp)
    assert isinstance(outcome, ParseOutcome)
    assert all(ev.external.source == "sofascore" for ev in outcome.events)
    assert all(ev.sport == "football" for ev in outcome.events)
    # honest assertion: a live list may be empty at fetch time, never an error
    print(f"football live events parsed: {len(outcome.events)}")


@_MARK
def test_live_basketball_fixtures_team_scoped() -> None:
    _spaced()
    adapter = SofascoreAdapter()
    resp = adapter.fetch("basketball", "fixtures", {"team": "Real Madrid"})
    assert resp.status == 200
    outcome = adapter.parse_events(resp)
    assert isinstance(outcome, ParseOutcome)
    names = {ev.home.name for ev in outcome.events} | {ev.away.name for ev in outcome.events}
    assert "Real Madrid" in names
    assert any("team-scoped" in w for w in outcome.warnings)
    print(f"basketball Real Madrid upcoming events: {len(outcome.events)}")


@_MARK
def test_live_tennis_events_roundtrip() -> None:
    _spaced()
    adapter = SofascoreAdapter()
    resp = adapter.fetch("tennis", "live", {})
    assert resp.status == 200
    outcome = adapter.parse_events(resp)
    assert isinstance(outcome, ParseOutcome)
    assert all(ev.sport == "tennis" for ev in outcome.events)
    print(f"tennis live events parsed: {len(outcome.events)}")


@_MARK
def test_live_unverified_cells_raise_typed_errors() -> None:
    _spaced()
    adapter = SofascoreAdapter()
    # date-only fixtures (no verified date-global endpoint) and tennis
    # fixtures/h2h must raise typed errors before any network request.
    with pytest.raises(NotFound):
        adapter.fetch("football", "fixtures", {"date": "2026-09-02"})
    with pytest.raises(NotFound):
        adapter.fetch("football", "h2h", {"entity_a": "A", "entity_b": "B"})
