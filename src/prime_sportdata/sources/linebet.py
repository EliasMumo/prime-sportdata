"""Linebet adapter — probe-driven (2026-09-09).

Linebet (linebet.com) is a 1xBet-platform bookmaker whose public JSON
endpoints are served without authentication.  Live-verified this session:

* ``GET /service-api/LineFeed/Get1x2_VZip?sports=1&count=500&lng=en&tf=2200000
  &tz=3&mode=4&country=87&partner=189&getEmpty=true`` -> ``{Success, Value}``
  with the upcoming football slate.  Each event carries ``CI`` (line game id),
  ``I`` (statistics game id), ``O1``/``O2`` (team names), ``L``/``LE``/``CN``
  (league and region), ``S`` (kickoff unix seconds), ``E`` (flat main-market
  odds) and ``WP`` (the site's own win probabilities P1/PX/P2 — recorded as
  evidence, never used as a model input).
* ``GET /service-api/LineFeed/GetGameZip?id={CI}&lng=en&isSubGames=true
  &GroupEvents=true&countevents=250&grMode=4&partner=189&topGroups=&country=87
  &marketType=1&isNewBuilder=true`` -> ``{Success, Value: {..., GE: [groups]}}``
  with the full additional-market groups for one game.
* ``GET /service-api/statisticfeed/api/v1/Game/h2h?id={I}&lng=en&ref=189
  &fcountry=87&gr=650`` -> ``{teams, gameShorts, entity}`` head-to-head
  history: past meetings (scores, halves, cards, winner, tournament title) and
  aggregated H2H counters (wins/draws/team stats).  ``id`` is the statistics
  game id from the list event's ``I`` field.

Market decoding (1xBet group conventions, verified against a live UCL event):

* flat list ``E``: 1X2 — type 1 = home, 2 = draw, 3 = away (cross-checked
  against the site's own WP probabilities);
* group 8: double chance — three columns in 1X/12/X2 order;
* group 19: both teams to score — first column holds both rows
  (type 180 = yes, 11273 = no);
* group 17: totals — over-lines / under-lines columns;
* group 8863: correct score — home-win / draw / away-win score columns with
  ``P = home + away/1000`` (e.g. 2.001 = 2:1, 0.002 = 0:2).  The site lists
  only the scores it prices (no OTHER bucket), so an incomplete 0..4 grid is
  shipped as-is and completeness stays the consumer's call.

Deliberately NOT shipped: the HT/FT group (11412) exists but its mixed
outcome encoding (plain rows + ``P=2`` second-half rows in the same column)
was not unambiguously decodable from the live capture — SPEC honesty rule:
unverified decodes never ship.

Only endpoints that were live-fetched AND successfully parsed during the
2026-09-09 probe session are shipped as data paths (SPEC: every shipped URL
must have been verified by a live fetch).  The adapter is football-only:
only football paths were verified.

Retry discipline (SPEC ban-risk policy): max 2 retries, exponential backoff
with jitter, ONLY on transport timeouts/errors and HTTP 5xx — never on
403/429.  One static realistic browser UA, no rotation, single host
(``linebet.com``).  Detail requests (GetGameZip per game) are paced at least
``DETAIL_MIN_INTERVAL_S`` apart with an injectable clock/sleep, mirroring the
Betika adapter.  No date filter exists on the verified list endpoint: dated
queries raise ``NotFound`` so the engine failover can answer from a
date-capable source.
"""

from __future__ import annotations

import json
import math
import random
import time
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx

from prime_sportdata.errors import (
    BadRequest,
    NoData,
    NotFound,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.models import (
    Competition,
    Event,
    EventStatus,
    ExternalRef,
    OddsQuote,
    Team,
)
from prime_sportdata.sources.base import (
    ParseOutcome,
    SourceAdapter,
    SourceResponse,
    parse_json_response,
    parse_warning,
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://linebet.com/en/line/football",
}

BASE_URL = "https://linebet.com"
_LIST_PATH = "/service-api/LineFeed/Get1x2_VZip"
_DETAIL_PATH = "/service-api/LineFeed/GetGameZip"
_H2H_PATH = "/service-api/statisticfeed/api/v1/Game/h2h"

CONNECT_TIMEOUT_S = 15.0
MAX_RETRIES = 2
_BACKOFF_BASE_S = 1.5
_JITTER_MAX_S = 0.8
LIST_PAGE_SIZE = 500
MAX_DETAIL_REQUESTS = 24
DETAIL_MIN_INTERVAL_S = 1.0
# Wall-clock budget for the whole detail phase.  The hosted worker must
# answer the odds/odds_linebet call well inside the proxy timeout (Render
# free tier kills at ~100s); slower egress simply yields fewer details.
DETAIL_BUDGET_S = 45.0

_VIRTUAL_MARKERS = ("cyber", "virtual", "esoccer", "e-football", "zoom")

_FOOTBALL = "football"
_H2H_CATEGORY = "h2h"
_ODDS_CATEGORY = "odds"
# Explicit linebet-only catalog row; behaves exactly like the odds category.
_ODDS_LINEBET_CATEGORY = "odds_linebet"

# 1xBet-style group ids used to decode the verified market groups.
_GROUP_DOUBLE_CHANCE = 8
_GROUP_BTTS = 19
_GROUP_TOTALS = 17
_GROUP_CORRECT_SCORE = 8863

_DC_LABELS = ("1X", "12", "X2")


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff seconds for one retry gap (jittered)."""
    return _BACKOFF_BASE_S * (2**attempt) + random.uniform(0.0, _JITTER_MAX_S)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _kickoff_iso(unix_seconds: object) -> str | None:
    """Unix-seconds kickoff -> ISO-8601 UTC, or None when unusable."""
    if isinstance(unix_seconds, bool) or not isinstance(unix_seconds, (int, float)):
        return None
    try:
        value = float(unix_seconds)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _normalized_name(value: str) -> str:
    """Light team-name normalization for h2h lookups (honest best-effort).

    Case-fold, strip, and fold diacritics so "Atlético" matches "Atletico".
    """
    folded = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(ascii_only.casefold().split())


def _clean_odds(raw: object) -> float | None:
    """Decimal odds > 1.0 from a JSON value, or None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 1.0 else None


def _flat_1x2(event: Mapping[str, Any]) -> dict[str, float] | None:
    """1X2 from the flat main-market list: type 1 = home, 2 = draw, 3 = away.

    Verified against a live UCL event where the site's own win probabilities
    (WP) matched the quoted 1X2 exactly.
    """
    entries = event.get("E")
    if not isinstance(entries, list):
        return None
    prices: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        market_type = entry.get("T")
        odds = _clean_odds(entry.get("C"))
        if market_type not in (1, 2, 3) or odds is None:
            continue
        prices[{1: "1", 2: "X", 3: "2"}[market_type]] = odds
    return prices if set(prices) == {"1", "X", "2"} else None


def _event_teams_match(event: Mapping[str, Any], home: str, away: str) -> bool:
    event_home = _normalized_name(str(event.get("O1") or ""))
    event_away = _normalized_name(str(event.get("O2") or ""))
    return event_home == home and event_away == away


def _event_teams_match_swapped(event: Mapping[str, Any], home: str, away: str) -> bool:
    event_home = _normalized_name(str(event.get("O1") or ""))
    event_away = _normalized_name(str(event.get("O2") or ""))
    return event_home == away and event_away == home


def _double_chance_prices(group: Mapping[str, Any]) -> dict[str, float] | None:
    """Group 8: three columns in 1X/12/X2 order, one row each."""
    columns = group.get("E")
    if not isinstance(columns, list) or len(columns) != 3:
        return None
    prices: dict[str, float] = {}
    for label, column in zip(_DC_LABELS, columns, strict=True):
        if not isinstance(column, list) or len(column) != 1 or not isinstance(column[0], dict):
            return None
        odds = _clean_odds(column[0].get("C"))
        if odds is None:
            return None
        prices[label] = odds
    return prices


def _btts_prices(group: Mapping[str, Any]) -> dict[str, float] | None:
    """Group 19: first column holds both rows (type 180 = yes, 11273 = no)."""
    columns = group.get("E")
    if not isinstance(columns, list) or not columns:
        return None
    column = columns[0] if isinstance(columns[0], list) else columns
    if not isinstance(column, list) or not column:
        return None
    prices: dict[str, float] = {}
    for entry in column:
        if not isinstance(entry, dict):
            continue
        odds = _clean_odds(entry.get("C"))
        if odds is None:
            continue
        if entry.get("T") == 180:
            prices["yes"] = odds
        elif entry.get("T") == 11273:
            prices["no"] = odds
    return prices if set(prices) == {"yes", "no"} else None


class LinebetAdapter(SourceAdapter):
    """Linebet odds + head-to-head adapter (football, JSON API)."""

    source: ClassVar[str] = "linebet"

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Injectable for pacing/retry symmetry with the Betika adapter; the
        # engine's per-host rate limiter paces whole adapter calls.
        self._clock = clock
        self._sleep = sleep
        self._last_detail_at: float | None = None

    # -- contract -----------------------------------------------------------

    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        sport, category = str(sport), str(category)
        if sport != _FOOTBALL:
            raise NotFound(
                f"linebet ships football-only paths; no verified {sport!r} path",
                source=self.source,
            )
        if category not in {_H2H_CATEGORY, _ODDS_CATEGORY, _ODDS_LINEBET_CATEGORY}:
            raise NotFound(
                f"linebet ships h2h and odds categories only; no verified "
                f"{category!r} endpoint",
                source=self.source,
            )
        if (params.get("date") or "").strip():
            # The verified endpoint is the site's own upcoming slate; there is
            # no verified date-filtered URL shape.
            raise NotFound(
                "linebet date filtering is unverified; the shipped endpoint is "
                "the upcoming slate",
                source=self.source,
            )
        list_resp = self._request(
            f"{BASE_URL}{_LIST_PATH}",
            params={
                "sports": "1",
                "count": str(LIST_PAGE_SIZE),
                "lng": "en",
                "tf": "2200000",
                "tz": "3",
                "mode": "4",
                "country": "87",
                "partner": "189",
                "getEmpty": "true",
            },
        )
        try:
            list_payload = list_resp.json()
        except ValueError as exc:
            raise NoData(f"linebet matches payload is not JSON: {exc}", source=self.source) from exc
        events = list_payload.get("Value") if isinstance(list_payload, dict) else None
        if not isinstance(events, list) or not events:
            raise NoData("linebet matches payload lacks Value[]", source=self.source)
        if category in {_ODDS_CATEGORY, _ODDS_LINEBET_CATEGORY}:
            details: dict[str, Any] = {}
            detail_skipped: list[str] = []
            detail_started = self._clock()
            for event in self._select_detail_rows(events):
                elapsed = self._clock() - detail_started
                if elapsed >= DETAIL_BUDGET_S:
                    detail_skipped.append("detail-budget")
                    break
                game_id = event.get("CI")
                if isinstance(game_id, bool) or not isinstance(game_id, (int, float, str)):
                    detail_skipped.append("no-game-id")
                    continue
                detail = self._fetch_detail(str(game_id))
                if detail is None:
                    detail_skipped.append(str(game_id))
                    continue
                details[str(game_id)] = detail
            assembled: dict[str, Any] = {
                "kind": "odds",
                "payload": list_payload,
                "details": details,
                "detail_skipped": detail_skipped,
            }
            return SourceResponse(
                source=self.source,
                payload=json.dumps(assembled),
                url=f"{BASE_URL}{_LIST_PATH}",
                status=200,
                fetched_at=_utc_now_iso(),
            )
        home = _normalized_name(params.get("entity_a") or "")
        away = _normalized_name(params.get("entity_b") or "")
        if not home or not away:
            raise BadRequest("h2h requires both entity_a and entity_b", source=self.source)
        matched = next(
            (event for event in events if _event_teams_match(event, home, away)),
            None,
        )
        swapped = False
        if matched is None:
            matched = next(
                (event for event in events if _event_teams_match_swapped(event, home, away)),
                None,
            )
            swapped = matched is not None
        if matched is None:
            # No upcoming fixture for this pair on linebet's slate.  That is
            # NOT a resolved-empty answer for the service: the score adapters
            # hold full h2h databases and can still serve historical pairs.
            # NotFound keeps the engine failover moving.
            raise NotFound(
                f"no upcoming linebet fixture between {params.get('entity_a')} "
                f"and {params.get('entity_b')}; failover for historical pairs",
                source=self.source,
            )
        stat_id = matched.get("I")
        if isinstance(stat_id, bool) or not isinstance(stat_id, (int, float, str)):
            raise NoData("matched linebet match lacks a statistics game id", source=self.source)
        h2h_resp = self._request(
            f"{BASE_URL}{_H2H_PATH}",
            params={
                "id": str(stat_id),
                "lng": "en",
                "ref": "189",
                "fcountry": "87",
                "gr": "650",
            },
        )
        try:
            h2h_payload = h2h_resp.json()
        except ValueError as exc:
            raise NoData(f"linebet h2h payload is not JSON: {exc}", source=self.source) from exc
        assembled = {
            "kind": "h2h",
            "match": matched,
            "swapped": swapped,
            "payload": h2h_payload,
        }
        return SourceResponse(
            source=self.source,
            payload=json.dumps(assembled),
            url=f"{BASE_URL}{_H2H_PATH}",
            status=200,
            fetched_at=_utc_now_iso(),
        )

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        """Decode the assembled h2h payload into normalized meeting events."""
        payload = parse_json_response(resp)
        if not isinstance(payload, dict) or payload.get("kind") != "h2h":
            raise NoData("linebet h2h response lacks its assembled envelope", source=resp.source)
        match = payload.get("match")
        data = payload.get("payload")
        if not isinstance(match, dict) or not isinstance(data, dict):
            raise NoData("linebet h2h payload is malformed", source=resp.source)
        teams = data.get("teams")
        game_shorts = data.get("gameShorts")
        entity = data.get("entity")
        if not isinstance(teams, list) or not isinstance(game_shorts, list):
            raise NoData("linebet h2h payload lacks teams/gameShorts", source=resp.source)
        if not isinstance(entity, dict):
            raise NoData("linebet h2h payload lacks the entity aggregate", source=resp.source)
        # gameShorts mixes both teams' recent form games with true head-to-head
        # meetings; entity.gameIds lists the true meetings (verified live).
        h2h_ids = {str(game_id) for game_id in entity.get("gameIds") or []}
        team_titles = {
            str(team.get("id")): str(team.get("title") or "").strip()
            for team in teams
            if isinstance(team, dict) and team.get("id") is not None
        }
        match_competition = Competition(
            name=str(match.get("LE") or match.get("L") or "").strip(),
            country=str(match.get("CN") or "").strip() or None,
        )
        warnings = [
            parse_warning(
                "linebet h2h meetings are scoped to the site's upcoming-match "
                "lookup; per-team history endpoints are unverified",
                resp,
            ),
        ]
        events: list[Event] = []
        for short in game_shorts:
            if not isinstance(short, dict):
                continue
            source_id = short.get("id")
            if isinstance(source_id, bool) or source_id is None:
                continue
            if str(source_id) not in h2h_ids:
                continue  # team-form game, not a head-to-head meeting
            home_name = team_titles.get(str(short.get("team1") or "")) or "Team 1"
            away_name = team_titles.get(str(short.get("team2") or "")) or "Team 2"
            kickoff = _kickoff_iso(short.get("dateStart"))
            score1 = short.get("score1")
            score2 = short.get("score2")
            if isinstance(score1, bool) or not isinstance(score1, (int, float)):
                score1 = None
            if isinstance(score2, bool) or not isinstance(score2, (int, float)):
                score2 = None
            status: EventStatus = (
                "finished" if score1 is not None and score2 is not None else "scheduled"
            )
            tournament = str(short.get("tournamentTitle") or "").strip()
            meeting_competition = Competition(
                name=tournament or match_competition.name,
                country=match_competition.country,
            )
            source_id = short.get("id")
            external = ExternalRef(
                source=self.source,
                source_event_id=str(source_id) if source_id is not None else "",
                source_url=f"{BASE_URL}/en/line/football",
            )
            if not external.source_event_id:
                continue
            events.append(
                Event(
                    sport=_FOOTBALL,  # type: ignore[arg-type]
                    external=external,
                    start_time_utc=kickoff,
                    status=status,
                    home=Team(name=home_name, score=int(score1) if score1 is not None else None),
                    away=Team(name=away_name, score=int(score2) if score2 is not None else None),
                    competition=meeting_competition,
                )
            )
        events.sort(key=lambda event: event.start_time_utc or "")
        if not events:
            warnings.append(
                parse_warning(
                    "linebet h2h: no recorded meetings between these teams",
                    resp,
                )
            )
        return ParseOutcome(events=events, warnings=warnings)

    def parse_odds(self, resp: SourceResponse) -> ParseOutcome:
        """Decode the assembled slate into normalized quotes, one per market."""
        payload = parse_json_response(resp)
        if not isinstance(payload, dict) or payload.get("kind") != "odds":
            raise NoData("linebet odds response lacks its assembled envelope", source=resp.source)
        data = payload.get("payload")
        events = data.get("Value") if isinstance(data, dict) else None
        if not isinstance(events, list):
            raise NoData("linebet odds payload lacks Value[]", source=resp.source)
        details = payload.get("details")
        if not isinstance(details, dict):
            details = {}
        skipped = payload.get("detail_skipped")
        warnings = [
            parse_warning(
                "linebet odds are observed bookmaker market evidence; not an "
                "approved execution bookmaker by default",
                resp,
            ),
            parse_warning(
                "linebet kickoff timestamps are unix seconds; parsed as UTC",
                resp,
            ),
            parse_warning(
                "linebet HT/FT group (11412) is present but its outcome encoding "
                "was not unambiguously decodable; it is not shipped",
                resp,
            ),
        ]
        if isinstance(skipped, list) and skipped:
            warnings.append(
                parse_warning(
                    f"linebet detail requests skipped for {len(skipped)} game(s): "
                    f"{', '.join(str(item) for item in skipped[:5])}",
                    resp,
                )
            )
        quotes: list[OddsQuote] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            match_quotes = self._normalize_event(event, details)
            if match_quotes is not None:
                quotes.extend(match_quotes)
        return ParseOutcome(quotes=quotes, warnings=warnings)

    # -- internals ----------------------------------------------------------

    def _select_detail_rows(self, events: list[Any]) -> list[Mapping[str, Any]]:
        """Pick up to ``MAX_DETAIL_REQUESTS`` non-virtual rows for detail hits."""
        selected: list[Mapping[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            league = f"{event.get('LE') or ''} {event.get('L') or ''}".casefold()
            if any(marker in league for marker in _VIRTUAL_MARKERS):
                continue
            selected.append(event)
            if len(selected) >= MAX_DETAIL_REQUESTS:
                break
        return selected

    def _pace_detail_request(self) -> None:
        now = self._clock()
        if self._last_detail_at is not None:
            wait = DETAIL_MIN_INTERVAL_S - (now - self._last_detail_at)
            if wait > 0:
                self._sleep(wait)
        self._last_detail_at = self._clock()

    def _fetch_detail(self, game_id: str) -> dict[str, Any] | None:
        """One GetGameZip call, paced; None when the game has no detail row."""
        self._pace_detail_request()
        try:
            resp = self._request(
                f"{BASE_URL}{_DETAIL_PATH}",
                params={
                    "id": game_id,
                    "lng": "en",
                    "isSubGames": "true",
                    "GroupEvents": "true",
                    "countevents": "250",
                    "grMode": "4",
                    "partner": "189",
                    "topGroups": "",
                    "country": "87",
                    "marketType": "1",
                    "isNewBuilder": "true",
                },
            )
        except (NotFound, SourceUnavailable):
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        value = body.get("Value") if isinstance(body, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("GE"), list):
            return None
        return value

    def _normalize_event(
        self,
        event: Mapping[str, Any],
        details: Mapping[str, Any],
    ) -> list[OddsQuote] | None:
        home = str(event.get("O1") or "").strip()
        away = str(event.get("O2") or "").strip()
        source_id = event.get("CI")
        if not home or not away:
            return None
        if isinstance(source_id, bool) or not isinstance(source_id, (int, float, str)):
            return None
        kickoff = _kickoff_iso(event.get("S"))
        competition = Competition(
            name=str(event.get("LE") or event.get("L") or "").strip(),
            country=str(event.get("CN") or "").strip() or None,
        )
        external = ExternalRef(
            source=self.source,
            source_event_id=str(source_id),
            source_url=f"{BASE_URL}/en/line/football",
        )

        def quote(market: str, prices: dict[str, float]) -> OddsQuote:
            return OddsQuote(
                sport=_FOOTBALL,  # type: ignore[arg-type]
                external=external,
                start_time_utc=kickoff,
                home=home,
                away=away,
                competition=competition,
                market=market,
                bookmaker="linebet",
                prices=prices,
            )

        detail = details.get(str(source_id))
        groups = detail.get("GE") if isinstance(detail, dict) else None

        def group(group_id: int) -> Mapping[str, Any] | None:
            if not isinstance(groups, list):
                return None
            for entry in groups:
                if isinstance(entry, dict) and entry.get("G") == group_id:
                    return entry
            return None

        quotes: list[OddsQuote] = []
        main = _flat_1x2(event)
        if main is not None:
            quotes.append(quote("1x2", main))
        dc_group = group(_GROUP_DOUBLE_CHANCE)
        if dc_group is not None:
            dc = _double_chance_prices(dc_group)
            if dc is not None:
                quotes.append(quote("double_chance", dc))
        btts_group = group(_GROUP_BTTS)
        if btts_group is not None:
            btts = _btts_prices(btts_group)
            if btts is not None:
                quotes.append(quote("btts", btts))
        totals_group = group(_GROUP_TOTALS)
        if totals_group is not None:
            for market, prices in _totals_quotes(totals_group).items():
                quotes.append(quote(market, prices))
        cs_group = group(_GROUP_CORRECT_SCORE)
        if cs_group is not None:
            cs = _correct_score_quotes(cs_group)
            if cs is not None:
                quotes.append(quote("correct_score", cs))
        if not quotes:
            return None
        return quotes

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
                    self._sleep(_backoff_seconds(attempt))
                    attempt += 1
                    continue
                raise SourceUnavailable(
                    f"request failed after {attempt + 1} attempt(s): {exc} — {url}",
                    source=self.source,
                ) from exc
            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                self._sleep(_backoff_seconds(attempt))
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


def _totals_quotes(group: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """Group 17: over-lines column and under-lines column, paired per line."""
    columns = group.get("E")
    if not isinstance(columns, list) or len(columns) != 2:
        return {}
    over_entries = columns[0] if isinstance(columns[0], list) else []
    under_entries = columns[1] if isinstance(columns[1], list) else []
    over_by_line: dict[float, float] = {}
    under_by_line: dict[float, float] = {}
    for entries, target in ((over_entries, over_by_line), (under_entries, under_by_line)):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            line = entry.get("P")
            odds = _clean_odds(entry.get("C"))
            if isinstance(line, bool) or not isinstance(line, (int, float)) or odds is None:
                continue
            line_value = float(line)
            if math.isfinite(line_value) and line_value > 0:
                target[line_value] = odds
    markets: dict[str, dict[str, float]] = {}
    for line in sorted(set(over_by_line) & set(under_by_line)):
        key = f"total_{str(line).replace('.', '_')}"
        markets[key] = {"over": over_by_line[line], "under": under_by_line[line]}
    return markets


def _correct_score_quotes(group: Mapping[str, Any]) -> dict[str, float] | None:
    """Group 8863: home-win / draw / away-win score columns.

    ``P = home + away/1000`` (2.001 = 2:1, 0.002 = 0:2).  The site lists only
    the scores it prices, so the 0..4 grid may be incomplete; ship it as-is
    (completeness stays the consumer's call).
    """
    columns = group.get("E")
    if not isinstance(columns, list) or len(columns) != 3:
        return None
    prices: dict[str, float] = {}

    def add(entry: object) -> None:
        if not isinstance(entry, dict):
            return
        parameter = entry.get("P")
        odds = _clean_odds(entry.get("C"))
        if isinstance(parameter, bool) or not isinstance(parameter, (int, float)) or odds is None:
            return
        parameter_value = float(parameter)
        if not math.isfinite(parameter_value) or parameter_value < 0:
            return
        home_goals = int(parameter_value)
        away_goals = round((parameter_value - home_goals) * 1000)
        if away_goals < 0 or away_goals > 99:
            return
        prices[f"{home_goals}:{away_goals}"] = odds

    for column in columns:
        if not isinstance(column, list):
            return None
        for entry in column:
            add(entry)
    if len(prices) < 5:
        return None
    return {label: prices[label] for label in sorted(prices)}
