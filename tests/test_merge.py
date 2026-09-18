"""Offline tests for the cross-source football-results merge."""

from __future__ import annotations

from prime_sportdata.models import Event, ExternalRef, ScoreLine, Team
from prime_sportdata.sources.merge import canonical_team_name, merge_football_results


def _event(
    home: str,
    away: str,
    *,
    start: str | None = "2026-09-16T17:00:00+00:00",
    status: str = "finished",
    lines: tuple[tuple[str, int | None, int | None], ...] = (),
    source: str = "flashscore",
) -> Event:
    return Event(
        sport="football",
        external=ExternalRef(source=source, source_event_id=f"{source}-{home}"),
        start_time_utc=start,
        status=status,
        home=Team(name=home, score=2),
        away=Team(name=away, score=0),
        score_lines=[ScoreLine(period_label=label, home=h, away=a) for label, h, a in lines],
    )


def test_merge_adds_half_time_lines_to_matching_primary() -> None:
    primary = [
        _event("Barcelona", "Racing Santander", start="2026-09-16T19:30:00+00:00")
    ]
    secondary = [
        _event(
            "Barcelona",
            "Racing Santander",
            start="2026-09-16T21:20:00",
            lines=(("H1", 4, 1), ("FT", 7, 2)),
            source="livescore",
        )
    ]

    merged, warnings = merge_football_results(primary, secondary)

    assert len(merged) == 1
    labels = {line.period_label: (line.home, line.away) for line in merged[0].score_lines}
    assert labels["H1"] == (4, 1)
    assert labels["FT"] == (7, 2)
    assert any("enriched" in warning for warning in warnings)


def test_merge_appends_secondary_without_match() -> None:
    primary = [_event("Sevilla", "Celta")]
    secondary = [
        _event(
            "Levante",
            "Athletic Club",
            status="postponed",
            source="livescore",
        )
    ]

    merged, warnings = merge_football_results(primary, secondary)

    assert len(merged) == 2
    assert merged[1].home.name == "Levante"
    assert merged[1].status == "postponed"
    assert any("appended 1 livescore-only" in warning for warning in warnings)


def test_merge_matches_cross_source_name_spellings() -> None:
    primary = [_event("Atl. Madrid", "Osasuna")]
    secondary = [
        _event(
            "Atletico Madrid",
            "Osasuna",
            lines=(("H1", 1, 0),),
            source="livescore",
        )
    ]

    merged, _ = merge_football_results(primary, secondary)

    assert len(merged) == 1
    assert merged[0].score_lines[0].period_label == "H1"
    assert canonical_team_name("Atl. Madrid") == "atletico madrid"
    assert canonical_team_name("Dep. A Coruna") == "deportivo la coruna"


def test_merge_kickoff_window_blocks_wrong_pairing() -> None:
    primary = [_event("Barcelona", "Racing Santander", start="2026-09-16T19:30:00+00:00")]
    secondary = [
        _event(
            "Barcelona",
            "Racing Santander",
            start="2026-09-17T09:00:00",
            lines=(("H1", 0, 0),),
            source="livescore",
        )
    ]

    merged, warnings = merge_football_results(primary, secondary)

    assert len(merged) == 2  # appended, not merged: kickoffs 13.5 h apart
    assert not any("enriched" in warning for warning in warnings)
