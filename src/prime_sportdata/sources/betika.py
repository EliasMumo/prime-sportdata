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
(``api.betika.com``). Detail requests are bounded (``MAX_DETAIL_REQUESTS``)
and sequential, each >= 1 s apart via the engine rate limiter.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Mapping
from datetime import UTC, datetime
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
MAX_DETAIL_REQUESTS = 12
LIST_PAGE_SIZE = 100

_FOOTBALL = "football"
_ODDS_DETAILED_CATEGORY = "odds_detailed"
_SOCCER_SPORT_NAME = "Soccer"

# Market group ids this adapter ships (probe-verified labels 2026-09-08).
_MARKET_1X2 = "1"
_MARKET_TOTAL = "18"
_MARKET_CORRECT_SCORE = "45"
_MARKET_HTFT = "47"
_MARKET_DOUBLE_CHANCE = "10"

# Correct score completeness: a 0..4 home/away grid plus an explicit "OTHER"
# outcome is the site's complete enumeration (verified 26 selections).
_COMPLETE_SCORE_GRID = frozenset(f"{h}:{a}" for h in range(5) for a in range(5))
_CORRECT_SCORE_OTHER = "OTHER"


def _sleep_backoff(attempt: int) -> None:
    time.sleep(_BACKOFF_BASE_S * (2**attempt) + random.uniform(0.0, _JITTER_MAX_S))


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


class BetikaAdapter(SourceAdapter):
    """Betika odds adapter: football HTFT/correct-score/total/1X2 via JSON API."""

    source: ClassVar[str] = "betika"

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
        list_resp = self._request(
            f"{BASE_URL}{_LIST_PATH}",
            params={"page": "1", "limit": str(LIST_PAGE_SIZE), "tab": "upcoming", "sub_type_id": "1"},
        )
        try:
            list_payload = list_resp.json()
        except ValueError as exc:
            raise NoData(f"betika matches payload is not JSON: {exc}", source=self.source) from exc
        rows = list_payload.get("data") if isinstance(list_payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise NoData("betika matches payload lacks data[]", source=self.source)
        details: dict[str, dict[str, Any]] = {}
        fetched = 0
        for row in rows:
            if fetched >= MAX_DETAIL_REQUESTS:
                break
            if not isinstance(row, dict) or row.get("sport_name") != _SOCCER_SPORT_NAME:
                continue
            match_id = row.get("parent_match_id")
            if not match_id:
                continue
            detail_resp = self._request(
                f"{BASE_URL}{_DETAIL_PATH}",
                params={"parent_match_id": str(match_id)},
            )
            try:
                detail = detail_resp.json()
            except ValueError:
                continue  # unusable detail: skip this match, never fabricate
            details[str(match_id)] = detail
            fetched += 1
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
        for row in rows:
            if not isinstance(row, dict) or row.get("sport_name") != _SOCCER_SPORT_NAME:
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
        total_2_5 = markets.get(f"{_MARKET_TOTAL}:2.5")
        if total_2_5:
            quotes.append(quote("total_2_5", total_2_5))
        double_chance = markets.get(_MARKET_DOUBLE_CHANCE)
        if double_chance and len(double_chance) == 3:
            quotes.append(quote("double_chance", double_chance))
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
                if sub_type == _MARKET_TOTAL:
                    special = str(outcome.get("special_bet_value") or "")
                    if "total=2.5" not in special:
                        continue
                    if display.upper().startswith("OVER"):
                        markets.setdefault(f"{_MARKET_TOTAL}:2.5", {})["over"] = value
                    elif display.upper().startswith("UNDER"):
                        markets.setdefault(f"{_MARKET_TOTAL}:2.5", {})["under"] = value
                elif sub_type == _MARKET_DOUBLE_CHANCE:
                    mapped = {"1/X": "1X", "X/2": "X2", "1/2": "12"}.get(display, display)
                    markets.setdefault(_MARKET_DOUBLE_CHANCE, {})[mapped] = value
                elif sub_type == _MARKET_CORRECT_SCORE:
                    label = display.upper() if display.upper() == _CORRECT_SCORE_OTHER else display
                    markets.setdefault(_MARKET_CORRECT_SCORE, {})[label] = value
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
