"""Sport x category catalog: 18 rows (odds 2026-09-08, odds_detailed Betika
2026-09-08, odds_linebet 2026-09-09, lineups 2026-09-20), per-row query
params and default source failover orders (SPEC "Failover order").

Order rationale (documented so later stages can re-decide via override):

* football fixtures/results/live -> flashscore first (flashscore HTML probed
  HTTP 200 and has all three sports; SPEC table row 1).
* basketball/tennis, all categories -> sofascore first (structured JSON API;
  SPEC table row 2).
* football h2h -> sofascore first. The SPEC table lists football categories
  exhaustively ("fixtures/results/live") and leaves h2h out of the
  flashscore-first row; h2h is a structured lookup where sofascore's JSON API
  is the best first bet and flashscore's h2h data path was never probed. The
  order is overridable at runtime (``source_order(... override=...)``), so the
  engine can flip it from config without code change.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from prime_sportdata.models import Category, Sport

SOURCES: tuple[str, ...] = (
    "flashscore",
    "sofascore",
    "livescore",
    "betexplorer",
    "betika",
    "linebet",
    "betwinner",
    "1xbet_ke",
)

# Canonical orders (SPEC failover table; both start with the best first).
FLASHSCORE_FIRST: tuple[str, ...] = ("flashscore", "sofascore", "livescore")
SOFASCORE_FIRST: tuple[str, ...] = ("sofascore", "flashscore", "livescore")
# Odds are scraped from BetExplorer only (probe-verified 2026-09-08); the
# score adapters have no odds paths.  The override machinery below still
# allows pinning a different order if another odds source ships later.
BETEXPLORER_ODDS: tuple[str, ...] = ("betexplorer",)
# Detailed markets (HTFT / correct score / totals / double chance) come from
# Betika's public JSON API (probe-verified 2026-09-08); football only.
BETIKA_ODDS_DETAILED: tuple[str, ...] = ("betika",)
# Linebet serves football odds + head-to-head from its own public JSON API
# (probe-verified 2026-09-09).  Its h2h path is scoped to the site's upcoming
# slate (the adapter raises NotFound for pairs without an upcoming fixture, so
# failover proceeds to the score adapters for historical pairs).  Its odds
# path is an upcoming-slate list like Betika's, so it ships in the detailed
# category and as a BetExplorer fallback for the dated odds category.
LINEBET_H2H: tuple[str, ...] = ("linebet", "sofascore", "flashscore", "livescore")
LINEBET_ODDS: tuple[str, ...] = ("linebet",)
# Explicit linebet-only row so callers can consume linebet quotes even when
# the shared odds/odds_detailed rows are answered by their primary sources
# (failover stops at the first answering source).

# Per-row supported query params (SPEC "Query params" + API surface).
FIXTURES_RESULTS_PARAMS: tuple[str, ...] = ("date", "team", "league", "limit")
LIVE_PARAMS: tuple[str, ...] = ("team", "league", "limit")  # live ignores date
H2H_PARAMS: tuple[str, ...] = ("entity_a", "entity_b", "league", "limit")
ODDS_PARAMS: tuple[str, ...] = ("date", "league", "limit")  # day-wide odds lists
LINEUPS_PARAMS: tuple[str, ...] = ("team_a", "team_b", "limit")
# Betika serves its own upcoming list; no verified date filter, so the
# detailed category takes league/limit only (caller-side filtering warning).
ODDS_DETAILED_PARAMS: tuple[str, ...] = ("league", "limit")

LIMIT_DEFAULT = 50
# Flashscore day feeds are league-alphabetical and hold 1,700+ events on
# busy football days; a 500 cap silently dropped every league from England
# onward (2026-09-19: EPL/La Liga/etc. never reached the algo, so football
# categories published nothing).  The day feed is bounded (~2k events), so
# serve up to 5,000 rows and let callers consume the full slate.
LIMIT_MAX = 5000

_OVERRIDE_KEY = "{sport}.{category}"  # e.g. "football.fixtures"


@dataclass(frozen=True)
class CatalogRow:
    """One (sport, category) capability row."""

    sport: Sport
    category: Category
    params: tuple[str, ...]
    sources_default: tuple[str, ...]
    limit_default: int = LIMIT_DEFAULT
    limit_max: int = LIMIT_MAX


ROWS: tuple[CatalogRow, ...] = (
    CatalogRow("football", "fixtures", FIXTURES_RESULTS_PARAMS, FLASHSCORE_FIRST),
    CatalogRow("football", "live", LIVE_PARAMS, FLASHSCORE_FIRST),
    CatalogRow("football", "results", FIXTURES_RESULTS_PARAMS, FLASHSCORE_FIRST),
    CatalogRow("football", "h2h", H2H_PARAMS, LINEBET_H2H),
    CatalogRow("basketball", "fixtures", FIXTURES_RESULTS_PARAMS, SOFASCORE_FIRST),
    CatalogRow("basketball", "live", LIVE_PARAMS, SOFASCORE_FIRST),
    CatalogRow("basketball", "results", FIXTURES_RESULTS_PARAMS, SOFASCORE_FIRST),
    CatalogRow("basketball", "h2h", H2H_PARAMS, SOFASCORE_FIRST),
    CatalogRow("tennis", "fixtures", FIXTURES_RESULTS_PARAMS, SOFASCORE_FIRST),
    CatalogRow("tennis", "live", LIVE_PARAMS, SOFASCORE_FIRST),
    CatalogRow("tennis", "results", FIXTURES_RESULTS_PARAMS, SOFASCORE_FIRST),
    CatalogRow("tennis", "h2h", H2H_PARAMS, SOFASCORE_FIRST),
    CatalogRow("football", "odds", ODDS_PARAMS, ("betexplorer", "linebet", "betwinner", "1xbet_ke")),
    CatalogRow("basketball", "odds", ODDS_PARAMS, BETEXPLORER_ODDS),
    CatalogRow("tennis", "odds", ODDS_PARAMS, BETEXPLORER_ODDS),
    CatalogRow("football", "odds_detailed", ODDS_DETAILED_PARAMS, ("betika", "linebet")),
    CatalogRow("football", "odds_linebet", ODDS_DETAILED_PARAMS, LINEBET_ODDS),
    # 1xBet-family siblings with explicit rows (probe-verified 2026-09-24).
    CatalogRow("football", "odds_betwinner", ODDS_DETAILED_PARAMS, ("betwinner",)),
    CatalogRow("football", "odds_1xbet_ke", ODDS_DETAILED_PARAMS, ("1xbet_ke",)),
    # Pre-match team sheets (sofascore /event/{id}/lineups, live-probed
    # 2026-09-20): resolved either by the caller's sofascore event_id or by
    # two team names (search -> both next/0 pages -> shared upcoming event).
    CatalogRow("football", "lineups", LINEUPS_PARAMS, ("sofascore",)),
)


def get_row(sport: Sport, category: Category) -> CatalogRow:
    """Look up the catalog row for ``(sport, category)``."""
    for row in ROWS:
        if row.sport == sport and row.category == category:
            return row
    raise KeyError(f"no catalog row for {sport}/{category}")


def default_source_order(sport: Sport, category: Category) -> tuple[str, ...]:
    """SPEC failover order for ``(sport, category)``, best source first."""
    return get_row(sport, category).sources_default


def source_order(
    sport: Sport,
    category: Category,
    override: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    """Default failover order, unless ``override`` pins one.

    Override keys are ``"{sport}.{category}"`` strings (e.g.
    ``{"football.fixtures": ["livescore", "sofascore"]}``) so the engine can
    build them straight from config env vars. Unknown or empty overrides raise
    ValueError — a typo'd source name must fail loudly, never silently degrade
    failover.
    """
    if override:
        order = override.get(_OVERRIDE_KEY.format(sport=sport, category=category))
        if order is not None:
            validated = tuple(order)
            if not validated:
                raise ValueError(f"empty failover order for {sport}/{category}")
            for source in validated:
                if source not in SOURCES:
                    raise ValueError(
                        f"unknown source {source!r} in override for {sport}/{category}; "
                        f"known: {', '.join(SOURCES)}"
                    )
            return validated
    return default_source_order(sport, category)
