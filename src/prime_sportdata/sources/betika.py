"""Betika odds adapter — probe-driven (2026-09-08). See ``docs/source-health.md``.

Betika is a Kenyan bookmaker whose public JSON endpoints are served without
authentication.  It is the only probe-verified scraping path that carries
**half-time/full-time** and **correct score** markets, so it complements the
BetExplorer adapter (which ships 1x2/two-way only).

Only endpoints that were live-fetched AND successfully parsed during the
2026-09-08 probe session are shipped as data paths (SPEC: every shipped URL
must have been verified by a live fetch).

Network realities recorded that session
---------------------------------------
* ``GET https://api.betika.com/v1/uo/matches?page=1&limit=15&tab=upcoming&
  sub_type_id=1`` answered HTTP 200 with an upcoming-matches list (``data``
  array: ``home_team``, ``away_team``, ``parent_match_id``, ``start_time``
  as ``YYYY-MM-DD HH:MM:SS`` in the site's local (Africa/Nairobi) time,
  ``competition_name``, ``category`` (country), ``sport_name``, inline
  ``home_odd``/``neutral_odd``/``away_odd``).
* ``GET https://api.betika.com/v1/uo/match?parent_match_id=<id>`` answered
  HTTP 200 with the full market list (``data`` array of market groups):
  ``sub_type_id`` 1=1X2, 10=double chance, 18=total (``special_bet_value``
  carries ``total=X``), 45=correct score (``0:0``..``4:4`` plus ``OTHER``),
  47=HALFTIME/FULLTIME (all nine ``1/1``..``2/2`` selections), plus first-
  half/team markets that this adapter deliberately does not ship.
* The upcoming list mixes sports (``sub_type_id=1`` also matches ice hockey
  1X2); rows are filtered caller-side to ``sport_name == "Soccer"`` with a
  standing warning (same precedent as other adapters' caller-side filters).

Retry discipline (SPEC Ban-risk policy): max 2 retries, exponential backoff
with jitter, ONLY on transport timeouts/errors and HTTP 5xx — never on
403/429 (those map straight to ``SourceBlocked``/``RateLimited`` and trip the
engine breaker). One static realistic browser UA, no rotation, single host
(``api.betika.com``).

Coverage upgrade (2026-09-09, live-verified): the upcoming list is paginated
in kickoff order and serves ``limit=500``, so the adapter pages up to
``MAX_LIST_PAGES`` pages until the slate reaches ``LIST_HORIZON_HOURS`` ahead
instead of reading page 1 only.  Simulated rows (SRL/Zoom/eSoccer) are
excluded, and the bounded detail budget (``MAX_DETAIL_REQUESTS``) is spent on
the fitted model's leagues first (``(competition, country)`` pairs in
``_PRIORITY_LEAGUES``) so evening top-flight fixtures still get HTFT /
correct-score details when the day opens with minor-league matches.  Detail
requests are paced ``DETAIL_MIN_INTERVAL_S`` apart inside the adapter (the
engine limiter only paces whole adapter calls).
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from prime_sportdata.errors import (
    NoData,
    NotFound,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.models import Competition, ExternalRef, OddsQuote
from prime_sportdata.sources.base import (
    ParseOutcome,
    SourceAdapter,
    SourceResponse,
    parse_warning,
)

# One static realistic browser UA per source (SPEC fingerprint hygiene).
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.betika.com/",
}

BASE_URL = "https://api.betika.com"
_LIST_PATH = "/v1/uo/matches"
_DETAIL_PATH = "/v1/uo/match"

CONNECT_TIMEOUT_S = 15.0
MAX_RETRIES = 2
_BACKOFF_BASE_S = 1.5
_JITTER_MAX_S = 0.8

# Bounded politeness: the upcoming list plus a capped number of per-match
# detail requests per fetch.  Odds change slowly enough that 12 matches is a
# safe daily bound; the engine rate limiter keeps them >= 1 s apart.  The
# list page size is 100 because the upcoming feed mixes many sports and
# virtual football; only ``sport_name == "Soccer"`` rows are real matches.
MAX_DETAIL_REQUESTS = 24
LIST_PAGE_SIZE = 500
# Pagination is bounded for ban-risk discipline.  The verified list endpoint
# serves the requested page size (``limit=500`` live-verified 2026-09-09) and
# returns pages in kickoff order, so a few pages cover the next ~36 h even
# when the first page is dominated by virtual matches.
MAX_LIST_PAGES = 5
LIST_HORIZON_HOURS = 36.0
DETAIL_MIN_INTERVAL_S = 1.0

# Virtual matches Betika interleaves with real soccer ("Zoom Soccer",
# "eSoccer", and "SRL" simulated leagues).  SRL rows keep ``sport_name``
# ``"Soccer"``, so the row-level filter must exclude these markers too.
_VIRTUAL_MARKERS = ("srl", "zoom", "esoccer")

_FOOTBALL = "football"
_ODDS_DETAILED_CATEGORY = "odds_detailed"
_SOCCER_SPORT_NAME = "Soccer"

# Market group ids this adapter ships (probe-verified labels 2026-09-08).
_MARKET_1X2 = "1"
_MARKET_TOTAL = "18"
_MARKET_CORRECT_SCORE = "45"
_MARKET_HTFT = "47"
_MARKET_DOUBLE_CHANCE = "10"
# Evidence markets added 2026-09-09 (same live-verified detail payload):
# both teams to score, first-half markets, additional total lines, winning
# margin, exact goals, corner and booking totals.
_MARKET_BTTS = "29"
_MARKET_FIRST_HALF_1X2 = "60"
_MARKET_FIRST_HALF_DC = "63"
_MARKET_FIRST_HALF_TOTAL = "68"
_MARKET_WINNING_MARGIN = "15"
_MARKET_EXACT_GOALS = "21"
_MARKET_CORNERS_TOTAL = "166"
_MARKET_BOOKINGS_TOTAL = "139"

_TOTAL_SUB_TYPES = frozenset(
    {
        _MARKET_TOTAL,
        _MARKET_FIRST_HALF_TOTAL,
        _MARKET_CORNERS_TOTAL,
        _MARKET_BOOKINGS_TOTAL,
    }
)
_TOTAL_MARKET_PREFIX = {
    _MARKET_TOTAL: "total",
    _MARKET_FIRST_HALF_TOTAL: "first_half_total",
    _MARKET_CORNERS_TOTAL: "corners_total",
    _MARKET_BOOKINGS_TOTAL: "bookings_total",
}

# Correct score completeness: a 0..4 home/away grid plus an explicit "OTHER"
# outcome is the site's complete enumeration (verified 26 selections).
_COMPLETE_SCORE_GRID = frozenset(f"{h}:{a}" for h in range(5) for a in range(5))
_CORRECT_SCORE_OTHER = "OTHER"
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)

# Competitions the fitted PrimePredict football model can consume, keyed by
# Betika's own (competition_name, category/country) pair.  ``category`` is
# what separates the real English Premier League (country: England) from
# Egypt's Premier League (country: Egypt) and real UCL (country:
# International Clubs) from youth/SRL lookalikes — competition name alone is
# ambiguous.  Rows in these pairs get the bounded detail-fetch budget first
# so evening top-flight fixtures are priced before it runs out on morning
# minor-league matches.
_PRIORITY_LEAGUES = frozenset(
    {
        ("premier league", "england"),
        ("la liga", "spain"),
        ("laliga", "spain"),
        ("primera division", "spain"),
        ("serie a", "italy"),
        ("bundesliga", "germany"),
        ("ligue 1", "france"),
        ("eredivisie", "netherlands"),
        ("championship", "england"),
        ("uefa champions league", "international clubs"),
        ("uefa europa league", "international clubs"),
        ("uefa conference league", "international clubs"),
    }
)


def _sleep_backoff(attempt: int) -> None:
    time.sleep(_BACKOFF_BASE_S * (2**attempt) + random.uniform(0.0, _JITTER_MAX_S))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _start_time_utc(raw: str | None) -> str | None:
    """``YYYY-MM-DD HH:MM:SS`` in Africa/Nairobi -> UTC ISO, or None."""
    if not raw:
        return None
    try:
        local = datetime.fromisoformat(raw)
        nairobi = ZoneInfo("Africa/Nairobi")
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return local.replace(tzinfo=nairobi).astimezone(UTC).isoformat()


def _row_start_time_utc(row: Mapping[str, Any]) -> datetime | None:
    """Parsed aware-UTC kickoff for one list row, or None when unusable."""
    iso = _start_time_utc(str(row.get("start_time") or ""))
    if iso is None:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _total_line_key(special: str, display: str) -> str | None:
    """Normalized total line key (``2_5``) from a total-market outcome.

    Betika carries the line in ``special_bet_value`` (``total=2.5``) for goal
    totals; corner/booking displays embed it instead (``OVER 10.5``).
    """
    line = ""
    if "total=" in special:
        line = special.partition("total=")[2].strip()
    else:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*$", display)
        if match:
            line = match.group(1)
    if not line:
        return None
    try:
        float(line)
    except ValueError:
        return None
    return line.replace(".", "_")


def _is_virtual_row(row: Mapping[str, Any]) -> bool:
    """True when a list row is a simulated/virtual soccer match."""
    competition = str(row.get("competition_name") or "").casefold()
    home = str(row.get("home_team") or "").casefold()
    away = str(row.get("away_team") or "").casefold()
    return any(
        marker in competition or marker in home or marker in away
        for marker in _VIRTUAL_MARKERS
    )


def _row_priority(row: Mapping[str, Any]) -> int:
    """0 for the fitted model's leagues, 1 otherwise.

    Uses the row's ``(competition_name, category)`` pair because Betika reuses
    generic names across countries (Egypt's "Premier League", Armenia's
    "Premier League", ...) and labels youth/SRL feeds with senior-sounding
    competition names.
    """
    competition = " ".join(str(row.get("competition_name") or "").casefold().split())
    country = " ".join(str(row.get("category") or "").casefold().split())
    return 0 if (competition, country) in _PRIORITY_LEAGUES else 1


def _select_detail_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    max_requests: int,
    horizon: datetime,
) -> list[dict[str, Any]]:
    """Spend the detail budget across the whole slate, not just the head.

    The upcoming list is kickoff-ordered, so a naive first-N loop prices only
    the next few morning matches.  Selecting by competition priority first and
    kickoff second keeps evening top-flight fixtures inside the budget.
    """
    with_kickoffs: list[tuple[dict[str, Any], datetime]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("sport_name") != _SOCCER_SPORT_NAME or _is_virtual_row(row):
            continue
        kickoff = _row_start_time_utc(row)
        if kickoff is not None and kickoff <= horizon:
            with_kickoffs.append((dict(row), kickoff))
    with_kickoffs.sort(
        key=lambda item: (
            _row_priority(item[0]),
            item[1],
        )
    )
    return [row for row, _kickoff in with_kickoffs[:max_requests]]


class BetikaAdapter(SourceAdapter):
    """Betika odds adapter: football HTFT/correct-score/total/1X2 via JSON API."""

    source: ClassVar[str] = "betika"

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now_utc: Callable[[], datetime] = _utc_now,
        min_detail_interval_s: float = DETAIL_MIN_INTERVAL_S,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._now_utc = now_utc
        self._min_detail_interval_s = min_detail_interval_s
        self._last_detail_at: float | None = None

    def _pace_detail_request(self) -> None:
        """Keep detail hits >= ``min_detail_interval_s`` apart (injectable).

        The engine rate limiter paces whole adapter calls, not the many
        internal requests a paginated fetch makes, so the adapter must pace
        its own detail burst (SPEC ban-risk policy).
        """
        now = self._clock()
        if self._last_detail_at is not None:
            wait = self._min_detail_interval_s - (now - self._last_detail_at)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_detail_at = now

    # -- contract -----------------------------------------------------------

    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        sport, category = str(sport), str(category)
        if sport != _FOOTBALL:
            raise NotFound(
                f"betika adapter ships football-only odds; no verified {sport!r} path",
                source=self.source,
            )
        if category != _ODDS_DETAILED_CATEGORY:
            raise NotFound(
                f"betika ships the {_ODDS_DETAILED_CATEGORY!r} category only; "
                f"no verified {category!r} endpoint",
                source=self.source,
            )
        if (params.get("date") or "").strip():
            # The verified endpoint is the site's own upcoming list; past/future
            # date filtering has no verified URL shape.
            raise NotFound(
                "betika date filtering is unverified; the shipped endpoint is the "
                "site's upcoming list",
                source=self.source,
            )
        horizon = self._now_utc() + timedelta(hours=LIST_HORIZON_HOURS)
        collected: list[dict[str, Any]] = []
        for page_number in range(1, MAX_LIST_PAGES + 1):
            list_resp = self._request(
                f"{BASE_URL}{_LIST_PATH}",
                params={
                    "page": str(page_number),
                    "limit": str(LIST_PAGE_SIZE),
                    "tab": "upcoming",
                    "sub_type_id": "1",
                },
            )
            try:
                list_payload = list_resp.json()
            except ValueError as exc:
                raise NoData(
                    f"betika matches payload is not JSON: {exc}",
                    source=self.source,
                ) from exc
            page_rows = list_payload.get("data") if isinstance(list_payload, dict) else None
            if not isinstance(page_rows, list) or not page_rows:
                if page_number == 1:
                    raise NoData("betika matches payload lacks data[]", source=self.source)
                break
            collected.extend(row for row in page_rows if isinstance(row, dict))
            furthest_kickoff = max(
                (
                    kickoff
                    for row in collected
                    if (kickoff := _row_start_time_utc(row)) is not None
                ),
                default=None,
            )
            if furthest_kickoff is not None and furthest_kickoff >= horizon:
                break
        rows = collected
        detail_targets = _select_detail_rows(
            rows,
            max_requests=MAX_DETAIL_REQUESTS,
            horizon=horizon,
        )
        details: dict[str, dict[str, Any]] = {}
        for row in detail_targets:
            match_id = row.get("parent_match_id")
            if not match_id:
                continue
            self._pace_detail_request()
            detail_resp = self._request(
                f"{BASE_URL}{_DETAIL_PATH}",
                params={"parent_match_id": str(match_id)},
            )
            try:
                detail = detail_resp.json()
            except ValueError:
                continue  # unusable detail: skip this match, never fabricate
            details[str(match_id)] = detail
        assembled: dict[str, Any] = {"rows": rows, "details": details}
        return SourceResponse(
            source=self.source,
            payload=json.dumps(assembled),
            url=f"{BASE_URL}{_LIST_PATH}",
            status=200,
            fetched_at=_utc_now_iso(),
        )

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        raise NotFound(
            "betika has no verified fixtures/results data path (odds-only adapter)",
            source=resp.source,
        )

    def parse_odds(self, resp: SourceResponse) -> ParseOutcome:
        """Parse assembled list+details into normalized quotes, one per market."""
        try:
            if isinstance(resp.payload, (bytes, bytearray)):
                payload = json.loads(bytes(resp.payload).decode("utf-8"))
            elif isinstance(resp.payload, str):
                payload = json.loads(resp.payload)
            else:
                payload = resp.payload
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NoData(f"betika assembled payload is not JSON: {exc}", source=resp.source) from exc
        rows = payload.get("rows") if isinstance(payload, dict) else None
        details = payload.get("details") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not isinstance(details, dict):
            raise NoData("betika assembled payload lacks rows/details", source=resp.source)
        warnings: list[str] = [
            parse_warning(
                "upcoming list mixes sports; rows filtered caller-side to sport_name='Soccer'",
                resp,
            ),
            parse_warning(
                "kickoff times parsed as Africa/Nairobi (site-local timezone assumption)",
                resp,
            ),
            parse_warning(
                "betika odds are observed bookmaker market evidence; not an approved "
                "execution bookmaker",
                resp,
            ),
        ]
        quotes: list[OddsQuote] = []
        skipped: dict[str, int] = {}
        soccer_rows = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("sport_name") == _SOCCER_SPORT_NAME
        ]
        # Emit rows with fetched details first so a caller-side ``limit``
        # truncation keeps the detailed HTFT/correct-score quotes instead of
        # dropping them off the tail of the kickoff-ordered list.
        ordered_rows = sorted(
            soccer_rows,
            key=lambda row: (
                0 if str(row.get("parent_match_id") or "") in details else 1,
                _row_priority(row),
                _row_start_time_utc(row) or _FAR_FUTURE,
            ),
        )
        for row in ordered_rows:
            if _is_virtual_row(row):
                reason = "virtual soccer row (SRL/Zoom/eSoccer)"
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            match_id = row.get("parent_match_id")
            if not match_id:
                continue
            detail = details.get(str(match_id))
            try:
                match_quotes = self._normalize_match(row, detail)
            except _SkipMatch as exc:
                reason = str(exc)
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            quotes.extend(match_quotes)
        for reason, count in sorted(skipped.items()):
            warnings.append(parse_warning(f"skipped {count} match(es): {reason}", resp))
        return ParseOutcome(quotes=quotes, warnings=warnings)

    # -- internals ----------------------------------------------------------

    def _normalize_match(self, row: dict[str, Any], detail: dict[str, Any] | None) -> list[OddsQuote]:
        home = str(row.get("home_team") or "").strip()
        away = str(row.get("away_team") or "").strip()
        if not home or not away:
            raise _SkipMatch("missing team names")
        match_id = str(row["parent_match_id"])
        start_time_utc = _start_time_utc(str(row.get("start_time") or ""))
        competition_name = str(row.get("competition_name") or "").strip() or None
        country = str(row.get("category") or "").strip() or None
        competition = (
            Competition(name=competition_name, country=country) if competition_name else None
        )
        external = ExternalRef(
            source=self.source,
            source_event_id=match_id,
            source_url=f"{BASE_URL}{_DETAIL_PATH}?parent_match_id={match_id}",
        )

        def quote(market: str, prices: dict[str, float]) -> OddsQuote:
            return OddsQuote(
                sport="football",
                external=external,
                start_time_utc=start_time_utc,
                home=home,
                away=away,
                competition=competition,
                market=market,
                bookmaker="betika",
                prices=prices,
            )

        markets = self._market_map(detail)
        quotes: list[OddsQuote] = []
        # 1X2: prefer the detail market group; fall back to the list row prices.
        prices_1x2 = markets.get(_MARKET_1X2)
        if prices_1x2 is None:
            try:
                prices_1x2 = {
                    "1": float(row["home_odd"]),
                    "X": float(row["neutral_odd"]),
                    "2": float(row["away_odd"]),
                }
            except (KeyError, TypeError, ValueError):
                prices_1x2 = None
        if prices_1x2 and all(v > 1.0 for v in prices_1x2.values()):
            quotes.append(quote("1x2", prices_1x2))
        htft = markets.get(_MARKET_HTFT)
        if htft and len(htft) == 9:
            quotes.append(quote("htft", htft))
        correct_score = markets.get(_MARKET_CORRECT_SCORE)
        if correct_score and self._complete_score_map(correct_score):
            quotes.append(quote("correct_score", correct_score))
        btts = markets.get("btts")
        if btts and set(btts) == {"yes", "no"}:
            quotes.append(quote("btts", btts))
        for market_key in sorted(
            key
            for key in markets
            if key.startswith(
                ("total:", "first_half_total:", "corners_total:", "bookings_total:")
            )
        ):
            prices = markets[market_key]
            if set(prices) == {"over", "under"}:
                quotes.append(quote(market_key.replace(":", "_"), prices))
        double_chance = markets.get(_MARKET_DOUBLE_CHANCE)
        if double_chance and len(double_chance) == 3:
            quotes.append(quote("double_chance", double_chance))
        first_half_1x2 = markets.get("first_half_1x2")
        if first_half_1x2 and set(first_half_1x2) == {"1", "X", "2"}:
            quotes.append(quote("first_half_1x2", first_half_1x2))
        first_half_dc = markets.get("first_half_double_chance")
        if first_half_dc and len(first_half_dc) == 3:
            quotes.append(quote("first_half_double_chance", first_half_dc))
        winning_margin = markets.get("winning_margin")
        if winning_margin:
            quotes.append(quote("winning_margin", winning_margin))
        exact_goals = markets.get("exact_goals")
        if exact_goals:
            quotes.append(quote("exact_goals", exact_goals))
        return quotes

    @staticmethod
    def _complete_score_map(prices: dict[str, float]) -> bool:
        """Attest completeness: full 0..4 grid plus an explicit OTHER outcome."""
        keys = set(prices)
        return _COMPLETE_SCORE_GRID <= keys and _CORRECT_SCORE_OTHER in keys

    def _market_map(self, detail: dict[str, Any] | None) -> dict[str, dict[str, float]]:
        """Extract shipped markets from one match-detail payload."""
        markets: dict[str, dict[str, float]] = {}
        groups = detail.get("data") if isinstance(detail, dict) else None
        if not isinstance(groups, list):
            return markets
        for group in groups:
            if not isinstance(group, dict):
                continue
            sub_type = str(group.get("sub_type_id") or "")
            odds = group.get("odds")
            if not isinstance(odds, list) or sub_type not in {
                _MARKET_1X2,
                _MARKET_TOTAL,
                _MARKET_CORRECT_SCORE,
                _MARKET_HTFT,
                _MARKET_DOUBLE_CHANCE,
                _MARKET_BTTS,
                _MARKET_FIRST_HALF_1X2,
                _MARKET_FIRST_HALF_DC,
                _MARKET_FIRST_HALF_TOTAL,
                _MARKET_WINNING_MARGIN,
                _MARKET_EXACT_GOALS,
                _MARKET_CORNERS_TOTAL,
                _MARKET_BOOKINGS_TOTAL,
            }:
                continue
            for outcome in odds:
                if not isinstance(outcome, dict):
                    continue
                display = str(outcome.get("display") or "").strip()
                raw_value = outcome.get("odd_value")
                if not display or raw_value in (None, ""):
                    continue
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                if not value > 1.0:
                    continue
                if sub_type in _TOTAL_SUB_TYPES:
                    special = str(outcome.get("special_bet_value") or "")
                    line_key = _total_line_key(special, display)
                    if line_key is None:
                        continue
                    if display.upper().startswith("OVER"):
                        side = "over"
                    elif display.upper().startswith("UNDER"):
                        side = "under"
                    else:
                        continue
                    prefix = _TOTAL_MARKET_PREFIX[sub_type]
                    markets.setdefault(f"{prefix}:{line_key}", {})[side] = value
                elif sub_type in {_MARKET_DOUBLE_CHANCE, _MARKET_FIRST_HALF_DC}:
                    mapped = {"1/X": "1X", "X/2": "X2", "1/2": "12"}.get(display, display)
                    market_name = (
                        _MARKET_DOUBLE_CHANCE
                        if sub_type == _MARKET_DOUBLE_CHANCE
                        else "first_half_double_chance"
                    )
                    markets.setdefault(market_name, {})[mapped] = value
                elif sub_type == _MARKET_CORRECT_SCORE:
                    label = display.upper() if display.upper() == _CORRECT_SCORE_OTHER else display
                    markets.setdefault(_MARKET_CORRECT_SCORE, {})[label] = value
                elif sub_type == _MARKET_BTTS:
                    normalized = {"YES": "yes", "NO": "no"}.get(display.upper())
                    if normalized is not None:
                        markets.setdefault("btts", {})[normalized] = value
                elif sub_type == _MARKET_FIRST_HALF_1X2:
                    markets.setdefault("first_half_1x2", {})[display] = value
                elif sub_type == _MARKET_WINNING_MARGIN:
                    label = display.upper().replace(" ", "_")
                    markets.setdefault("winning_margin", {})[label] = value
                elif sub_type == _MARKET_EXACT_GOALS:
                    markets.setdefault("exact_goals", {})[display] = value
                else:
                    markets.setdefault(sub_type, {})[display] = value
        return markets

    # -- request ------------------------------------------------------------

    def _request(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        attempt = 0
        while True:
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(CONNECT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                    headers=_HEADERS,
                    follow_redirects=False,
                ) as client:
                    resp = client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt < MAX_RETRIES:
                    _sleep_backoff(attempt)
                    attempt += 1
                    continue
                raise SourceUnavailable(
                    f"request failed after {attempt + 1} attempt(s): {exc} — {url}",
                    source=self.source,
                ) from exc
            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                _sleep_backoff(attempt)
                attempt += 1
                continue
            if resp.status_code == 200:
                return resp
            if resp.status_code == 403:
                raise SourceBlocked(f"HTTP 403 from {url}", source=self.source)
            if resp.status_code == 429:
                raise RateLimited(f"HTTP 429 from {url}", source=self.source)
            if resp.status_code == 404:
                raise NotFound(f"HTTP 404 — path not on this API version: {url}", source=self.source)
            raise SourceUnavailable(f"unexpected HTTP {resp.status_code} from {url}", source=self.source)


class _SkipMatch(Exception):
    """One unusable match; counted into parse warnings, never guessed."""
