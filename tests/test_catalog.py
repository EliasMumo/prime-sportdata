"""Catalog tests: 20 rows (odds 2026-09-08; odds_detailed Betika 2026-09-08;
odds_betwinner/odds_1xbet_ke 2026-09-24; lineups 2026-09-20)."""

import pytest

from prime_sportdata.catalog import (
    BETEXPLORER_ODDS,
    FLASHSCORE_FIRST,
    LINEBET_H2H,
    ROWS,
    SOFASCORE_FIRST,
    get_row,
    source_order,
)

SPORTS: tuple[str, ...] = ("football", "basketball", "tennis")
CATEGORIES: tuple[str, ...] = ("fixtures", "live", "results", "h2h", "odds")


def test_exactly_twenty_rows():
    assert len(ROWS) == 20


def test_covers_full_sport_x_category_matrix_once():
    pairs = {(row.sport, row.category) for row in ROWS}
    expected = (
        {(s, c) for s in SPORTS for c in CATEGORIES}
        | {("football", "odds_detailed")}
        | {("football", "odds_linebet")}
        | {("football", "odds_betwinner")}
        | {("football", "odds_1xbet_ke")}
        | {("football", "lineups")}
    )
    assert pairs == expected


def test_football_fixtures_results_live_flashscore_first():
    for category in ("fixtures", "results", "live"):
        assert get_row("football", category).sources_default == FLASHSCORE_FIRST


def test_basketball_and_tennis_all_sofascore_first():
    for sport in ("basketball", "tennis"):
        for category in ("fixtures", "live", "results", "h2h"):
            assert get_row(sport, category).sources_default == SOFASCORE_FIRST


def test_odds_rows_betexplorer_first():
    # Football: betexplorer primary with linebet as fallback for the dated
    # odds category (linebet has no verified date filter).
    assert get_row("football", "odds").sources_default == (
        "betexplorer", "linebet", "betwinner", "1xbet_ke"
    )
    for sport in ("basketball", "tennis"):
        assert get_row(sport, "odds").sources_default == BETEXPLORER_ODDS
        assert get_row(sport, "odds").params == ("date", "league", "limit")


def test_odds_detailed_betika_first_linebet_fallback_football_only():
    assert get_row("football", "odds_detailed").sources_default == ("betika", "linebet")
    assert get_row("football", "odds_detailed").params == ("league", "limit")
    with pytest.raises(KeyError):
        get_row("basketball", "odds_detailed")


def test_odds_linebet_linebet_only_football():
    assert get_row("football", "odds_linebet").sources_default == ("linebet",)
    assert get_row("football", "odds_linebet").params == ("league", "limit")


def test_family_sibling_odds_rows_football_only():
    assert get_row("football", "odds_betwinner").sources_default == ("betwinner",)
    assert get_row("football", "odds_betwinner").params == ("league", "limit")
    assert get_row("football", "odds_1xbet_ke").sources_default == ("1xbet_ke",)
    assert get_row("football", "odds_1xbet_ke").params == ("league", "limit")
    with pytest.raises(KeyError):
        get_row("basketball", "odds_betwinner")


def test_default_source_order_matches_row():
    for sport in SPORTS:
        for category in CATEGORIES:
            assert source_order(sport, category) == get_row(sport, category).sources_default


def test_param_sets_per_row():
    for row in ROWS:
        params = set(row.params)
        assert "limit" in params
        if row.category == "lineups":
            # resolved by two team names; no league/date filters exist on the
            # verified lineups path
            assert {"team_a", "team_b"} <= params
            assert "date" not in params and "team" not in params and "league" not in params
            continue
        assert "league" in params
        if row.category in ("fixtures", "results"):
            assert {"date", "team"} <= params
        elif row.category == "live":
            assert "team" in params
            assert "date" not in params  # SPEC: live ignores date
        elif row.category == "h2h":
            assert {"entity_a", "entity_b"} <= params
            assert "team" not in params and "date" not in params
        elif row.category == "odds":
            assert "date" in params
            assert "team" not in params and "entity_a" not in params
        elif row.category == "odds_detailed":
            assert "league" in params and "limit" in params
            assert "date" not in params


def test_limits_default_50_max_5000():
    for row in ROWS:
        assert row.limit_default == 50
        assert row.limit_max == 5000


def test_missing_row_raises_key_error():
    with pytest.raises(KeyError):
        get_row("football", "standings")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        get_row("cricket", "fixtures")  # type: ignore[arg-type]


def test_override_changes_order_without_code_change():
    override = {"football.fixtures": ["livescore", "flashscore"]}
    assert source_order("football", "fixtures", override) == ("livescore", "flashscore")
    # Unrelated key falls back to the default.
    assert source_order("football", "live", override) == FLASHSCORE_FIRST


def test_override_rejects_unknown_or_empty_source():
    with pytest.raises(ValueError):
        source_order("football", "fixtures", {"football.fixtures": ["bogus"]})
    with pytest.raises(ValueError):
        source_order("football", "fixtures", {"football.fixtures": []})


def test_catalog_module_importable_lookup():
    from prime_sportdata import catalog

    assert catalog.SOURCES == (
        "flashscore",
        "sofascore",
        "livescore",
        "betexplorer",
        "betika",
        "linebet",
        "betwinner",
        "1xbet_ke",
    )
    assert len(catalog.ROWS) == 20


def test_football_h2h_linebet_first():
    # Linebet ships its own h2h for pairs with an upcoming fixture (probe-
    # verified 2026-09-09); the adapter raises NotFound for other pairs so
    # the engine failover still reaches the score adapters' full databases.
    assert get_row("football", "h2h").sources_default == LINEBET_H2H
