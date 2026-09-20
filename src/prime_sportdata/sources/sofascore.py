"""Sofascore adapter — probe-driven (2026-09-02). See ``docs/source-health.md``.

Only endpoints that were live-fetched AND successfully parsed during the
2026-09-02 probe session are shipped as data paths (SPEC: every shipped URL
must have been verified by a live fetch). Cells without a verified endpoint
raise typed errors with the evidence in the detail message — never fabricated
rows, never a parse of an unverified shape.

Network realities recorded that session
---------------------------------------
* IPv6 toward sofascore hosts blackholes on this network; the DNS A records
  served on 2026-09-02 for both hosts (151.101.3.52/.67.52/.131.52/.195.52)
  also time out at TCP, while ``151.101.175.52`` (the A record that answered
  the SPEC evidence probe earlier that day) answers both ``www.sofascore.com``
  and ``api.sofascore.com``. The adapter therefore (a) forces AF_INET and
  (b) tries the session-verified fallback address first, then live DNS
  results. Both shims are scoped to sofascore hostnames only so the rest of
  the process is unaffected.
* Date-global schedule endpoints (``/sport/{sport}/scheduled-events/{date}``,
  ``/sport/{sport}/events/{date}``, ``/sport/{sport}/finished-events/{date}``)
  return HTTP 404 on both hosts for all three sports — this API version no
  longer exposes them without a session token (out of scope, SPEC). Team-
  scoped endpoints (``/team/{id}/events/next|last/0``) are used when a
  ``team`` param is given; date-only asks raise ``NotFound``.
* Tennis event lists per player are not exposed: ``/team/{playerId}/events/
  next/0`` -> 404, and search results for tennis players carry no sport
  marker usable for entity resolution. Tennis live works.
* Head-to-head meeting lists are not exposed without a token either:
  ``/event/{id}/h2h/events`` -> 404 on both hosts. ``/event/{id}/h2h`` -> 200
  but carries only aggregate counts (``teamDuel``/``managerDuel``) with no
  side names or meeting events — insufficient to build the SPEC h2h envelope
  honestly, so the h2h category raises ``NotFound`` with this evidence.

Verified data paths (200 + parsed this session; recorded fixtures under
``tests/fixtures/sofascore/``):

* ``GET www.sofascore.com/api/v1/sport/{football|basketball|tennis}/events/live``
* ``GET www.sofascore.com/api/v1/team/{team_id}/events/next/0`` (fixtures)
* ``GET www.sofascore.com/api/v1/team/{team_id}/events/last/0`` (results)
* ``GET www.sofascore.com/api/v1/search/all?q={q}`` (team resolution)

Retry discipline (SPEC Ban-risk policy): max 2 retries, exponential backoff
with jitter, ONLY on transport timeouts/errors and HTTP 5xx — never on
403/429 (those map straight to ``SourceBlocked``/``RateLimited`` and trip the
engine breaker). One static realistic browser UA, no rotation. Team-scoped
pages return the source's next/last 30 regardless of date — a standing
warning is attached to such parses (date filtering is caller-side).
"""

from __future__ import annotations

import random
import socket
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx

from prime_sportdata.errors import (
    BadRequest,
    NoData,
    NotFound,
    PrimeSportDataError,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.models import Competition, Event, EventStatus, ExternalRef, ScoreLine, Team
from prime_sportdata.sources.base import (
    ParseOutcome,
    SourceAdapter,
    SourceResponse,
    parse_json_response,
    parse_warning,
    stamp_provenance,
)

# One static realistic browser UA per source (SPEC fingerprint hygiene).
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
}

BASE_WWW = "https://www.sofascore.com/api/v1"
BASE_API = "https://api.sofascore.com/api/v1"

# Session-verified IPv4 route to both hosts (2026-09-02; DNS answers for both
# hostnames currently time out from this network — see module docstring and
# docs/source-health.md). Tried first, live DNS results follow as fallbacks.
SOFASCORE_IPV4_FALLBACKS: tuple[str, ...] = ("151.101.175.52",)
_SOFASCORE_HOSTS: frozenset[str] = frozenset({"www.sofascore.com", "api.sofascore.com"})

CONNECT_TIMEOUT_S = 10.0
MAX_RETRIES = 2
_BACKOFF_BASE_S = 1.5
_JITTER_MAX_S = 0.8

_FOOTBALL = "football"
_BASKETBALL = "basketball"
_TENNIS = "tennis"
_SPORTS = (_FOOTBALL, _BASKETBALL, _TENNIS)
_CATEGORIES = ("fixtures", "live", "results", "h2h", "lineups")

# sofascore status.type -> SPEC EventStatus. Anything else: event skipped with
# a warning (shape change must surface, never be guessed silently).
_STATUS_MAP: dict[str, EventStatus] = {
    "notstarted": "scheduled",
    "inprogress": "live",
    "finished": "finished",
    "postponed": "postponed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
    "abandoned": "interrupted",
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --- IPv4-only, fallback-first getaddrinfo shim --------------------------------

_real_getaddrinfo: Any = socket.getaddrinfo
_getaddrinfo_lock = threading.Lock()


def _sofascore_getaddrinfo(
    host: str, port: int | str | None, *args: Any, **kwargs: Any
) -> list[Any]:
    """getaddrinfo replacement active while a sofascore request is in flight.

    For sofascore hosts only: answers AF_INET results with the session-
    verified fallback address first and live DNS results after (deduplicated),
    because IPv6 and the DNS answers of 2026-09-02 blackhole on this network
    while ``151.101.175.52`` answers. Any caller-supplied address family is
    overridden (positional or keyword form — sync httpx/httpcore passes it
    positionally via ``socket.create_connection``). Non-sofascore hosts are
    delegated untouched so the shim has no global side effects.
    """
    if host not in _SOFASCORE_HOSTS:
        return _real_getaddrinfo(host, port, *args, **kwargs)
    if args:
        args = (socket.AF_INET,) + args[1:]
    elif "family" in kwargs:
        kwargs["family"] = socket.AF_INET
    else:
        args = (socket.AF_INET,)
    results = _real_getaddrinfo(host, port, *args, **kwargs)
    ordered: list[Any] = []
    seen: set[str] = set()
    template = results[0] if results else (socket.AF_INET, socket.SOCK_STREAM, 0, "")
    for ip in (*SOFASCORE_IPV4_FALLBACKS, *(r[4][0] for r in results)):
        if ip in seen:
            continue
        seen.add(ip)
        ordered.append((*template[0:4], (ip, port)))
    return ordered


@contextmanager
def ipv4_only() -> Iterator[None]:
    """Scope the fallback-first AF_INET getaddrinfo shim to one request.

    ``socket.getaddrinfo`` is process-global, so installation is serialized
    with a lock; the shim itself only alters resolution of the two sofascore
    hostnames, so concurrently running non-sofascore fetches are unaffected.
    """
    with _getaddrinfo_lock:
        original = socket.getaddrinfo
        socket.getaddrinfo = _sofascore_getaddrinfo  # type: ignore[assignment]
        try:
            yield
        finally:
            socket.getaddrinfo = original


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter between retries (SPEC §5)."""
    time.sleep(_BACKOFF_BASE_S * (2**attempt) + random.uniform(0.0, _JITTER_MAX_S))


def _map_http_outcome(
    url: str, status: int, body: bytes, fetched_at: str, source: str
) -> SourceResponse:
    """Turn one final HTTP outcome into a response or a typed error.

    Pure function of the observed response (tested offline): 403/429/5xx/
    404/400 map to their typed errors; a 200 that served HTML is treated as a
    challenge/block signature; a 200 is returned verbatim (payload stays raw
    bytes — parsing happens later against the recorded response).
    """
    if status == 200:
        head = body[:512].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            raise SourceBlocked(
                f"HTTP 200 served HTML (challenge/block signature) at {url}", source=source
            )
        return SourceResponse(source=source, payload=body, url=url, status=200, fetched_at=fetched_at)
    if status == 403:
        raise SourceBlocked(f"HTTP 403 from {url}", source=source)
    if status == 429:
        raise RateLimited(f"HTTP 429 from {url}", source=source)
    if status == 404:
        raise NotFound(f"HTTP 404 — path not on this API version: {url}", source=source)
    if status == 400:
        raise BadRequest(f"HTTP 400 from {url}", source=source)
    if status >= 500:
        raise SourceUnavailable(f"HTTP {status} (retries exhausted) from {url}", source=source)
    raise SourceUnavailable(f"unexpected HTTP {status} from {url}", source=source)


class SofascoreAdapter(SourceAdapter):
    """Sofascore adapter: IPv4-forced httpx client, one static UA, typed errors."""

    source: ClassVar[str] = "sofascore"

    # -- contract -----------------------------------------------------------

    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        sport, category = str(sport), str(category)
        if sport not in _SPORTS:
            raise BadRequest(f"unknown sport {sport!r}", source=self.source)
        if category not in _CATEGORIES:
            raise BadRequest(f"unknown category {category!r}", source=self.source)
        if category == "h2h":
            raise self._h2h_unavailable()
        if category == "lineups":
            return self._fetch_lineups(sport, params)
        if category == "live":
            return self._request(self._live_url(sport))
        if sport == _TENNIS:
            raise self._tennis_unavailable(category)
        team = (params.get("team") or "").strip()
        if not team:
            raise self._no_team(sport, category)
        team_id = self._resolve_team(sport, team)
        page = "next/0" if category == "fixtures" else "last/0"
        return self._request(f"{BASE_WWW}/team/{team_id}/events/{page}")

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        data = parse_json_response(resp)
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise NoData(
                f"payload lacks an events[] list (top-level keys: "
                f"{sorted(data) if isinstance(data, dict) else type(data).__name__})",
                source=resp.source,
            )
        warnings: list[str] = []
        if "/team/" in resp.url:
            warnings.append(
                parse_warning(
                    "team-scoped endpoint returns the source's next/last event page "
                    "regardless of date — date filtering is caller-side",
                    resp,
                )
            )
        events: list[Event] = []
        skipped: dict[str, int] = {}
        for raw in data["events"]:
            if not isinstance(raw, dict):
                skipped["event entry is not an object"] = skipped.get("event entry is not an object", 0) + 1
                continue
            try:
                event = self._normalize_event(raw)
            except _SkipEvent as exc:
                reason = str(exc)
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            events.append(event)
        for reason, count in sorted(skipped.items()):
            warnings.append(parse_warning(f"skipped {count} event(s): {reason}", resp))
        return ParseOutcome(events=stamp_provenance(resp, events), warnings=warnings)

    # -- lineups ------------------------------------------------------------

    def _fetch_lineups(self, sport: str, params: Mapping[str, str]) -> SourceResponse:
        """Resolve two team names to their shared upcoming event, then fetch
        its pre-match team sheets (``/event/{id}/lineups``, live-probed
        2026-09-20).  Returns a combined SourceResponse carrying the two real
        team-event pages, the selected raw event and the lineups document.
        """
        if sport != _FOOTBALL:
            raise NotFound(
                f"{sport} lineups have no verified endpoint on this sofascore "
                f"API version — football only (probed 2026-09-20: "
                f"/event/{{id}}/lineups answered HTTP 200 for football events).",
                source=self.source,
            )
        team_a = (params.get("team_a") or "").strip()
        team_b = (params.get("team_b") or "").strip()
        if not team_a or not team_b:
            raise BadRequest("football lineups requires team_a and team_b", source=self.source)
        id_a = self._resolve_team(sport, team_a)
        id_b = self._resolve_team(sport, team_b)
        events_url = f"{BASE_WWW}/team/{{team_id}}/events/next/0"
        resp_a = self._request(events_url.format(team_id=id_a))
        resp_b = self._request(events_url.format(team_id=id_b))
        page_a = parse_json_response(resp_a)
        page_b = parse_json_response(resp_b)
        list_a = page_a.get("events") if isinstance(page_a, dict) else None
        list_b = page_b.get("events") if isinstance(page_b, dict) else None
        if not isinstance(list_a, list) or not isinstance(list_b, list):
            raise NoData("team event pages lack events[] lists", source=self.source)
        shared = _shared_upcoming_event(list_a, list_b, id_a, id_b)
        if shared is None:
            raise NotFound(
                f"no shared upcoming fixture found on sofascore for "
                f"{team_a!r} ({id_a}) vs {team_b!r} ({id_b}): both next-event "
                f"pages answered but carry no common event id",
                source=self.source,
            )
        event_id = shared["id"]
        lineups_url = f"{BASE_API}/event/{event_id}/lineups"
        lineups_resp = self._request(lineups_url)
        payload = parse_json_response(lineups_resp)
        if not isinstance(payload, dict):
            raise NoData(f"lineups endpoint answered non-object JSON at {lineups_url}", source=self.source)
        return SourceResponse(
            source=self.source,
            payload={
                "team_a_page": page_a,
                "team_b_page": page_b,
                "selected_event": shared,
                "lineups": payload,
            },
            url=lineups_url,
            status=lineups_resp.status,
            fetched_at=lineups_resp.fetched_at,
        )

    def parse_lineups(self, resp: SourceResponse) -> ParseOutcome:
        """Normalize a ``_fetch_lineups`` response into the event + sheets."""
        data = parse_json_response(resp)
        if not isinstance(data, dict):
            raise NoData("lineups payload is not an object", source=resp.source)
        raw_lineups = data.get("lineups")
        if not isinstance(raw_lineups, dict) or "home" not in raw_lineups or "away" not in raw_lineups:
            raise NoData(
                f"lineups payload lacks home/away sheets (keys: {sorted(raw_lineups) if isinstance(raw_lineups, dict) else type(raw_lineups).__name__})",
                source=resp.source,
            )
        raw_event = data.get("selected_event")
        if not isinstance(raw_event, dict):
            raise NoData("lineups payload lacks the selected event", source=resp.source)
        warnings: list[str] = []
        try:
            event = self._normalize_event(raw_event)
        except _SkipEvent as exc:
            raise NoData(f"selected event cannot be normalized: {exc}", source=resp.source) from exc
        if isinstance(raw_lineups.get("confirmed"), bool) and not raw_lineups["confirmed"]:
            warnings.append(
                parse_warning(
                    "lineups are provisional (source confirmed=false) — "
                    "treat as team-news hints, never confirmed sheets",
                    resp,
                )
            )
        return ParseOutcome(
            events=stamp_provenance(resp, [event]),
            lineups=_normalize_lineups_document(raw_lineups),
            warnings=warnings,
        )

    # -- verified endpoints -------------------------------------------------

    def _live_url(self, sport: str) -> str:
        if sport not in _SPORTS:
            raise BadRequest(f"unknown sport {sport!r}", source=self.source)
        return f"{BASE_WWW}/sport/{sport}/events/live"

    def _request(self, url: str) -> SourceResponse:
        """GET ``url`` with retry discipline; returns or raises a typed error."""
        attempt = 0
        while True:
            try:
                with (
                    ipv4_only(),
                    httpx.Client(
                        timeout=httpx.Timeout(CONNECT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                        headers=_HEADERS,
                        follow_redirects=False,
                    ) as client,
                ):
                    resp = client.get(url)
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
            return _map_http_outcome(url, resp.status_code, resp.content, _utc_now_iso(), self.source)

    def _resolve_team(self, sport: str, team: str) -> int:
        """Resolve a team name to a sofascore team id via the search endpoint.

        Search is the verified endpoint (2026-09-02); only entities whose
        payload marks them as the requested sport (``type == 0`` + sport slug)
        are candidates — never a wrong-sport guess.
        """
        url = f"{BASE_WWW}/search/all?{httpx.QueryParams({'q': team})}"
        resp = self._request(url)
        data = parse_json_response(resp)
        results = data.get("results") if isinstance(data, dict) else None
        candidates: list[dict[str, Any]] = []
        if isinstance(results, list):
            for row in results:
                entity = row.get("entity") if isinstance(row, dict) else None
                if not isinstance(entity, dict):
                    continue
                if entity.get("type") != 0:
                    continue
                if (entity.get("sport") or {}).get("slug") != sport:
                    continue
                if isinstance(entity.get("name"), str):
                    candidates.append(entity)
        if not candidates:
            raise NoData(
                f"no {sport} team found for {team!r} in sofascore search "
                f"(search endpoint answered 200 at {url})",
                source=self.source,
            )
        chosen = self._pick_team_entity(candidates, team)
        team_id = chosen.get("id")
        if not isinstance(team_id, int):
            raise NoData(
                f"search hit for {team!r} carried no usable team id ({url})",
                source=self.source,
            )
        return team_id

    @staticmethod
    def _pick_team_entity(candidates: Sequence[dict[str, Any]], query: str) -> dict[str, Any]:
        """Best candidate by name match; falls back to search relevance order.

        Substring matches prefer the shortest name (a bare club short-name
        query like "barcelona" must resolve FC Barcelona, not the longer
        "Barcelona SC Guayaquil").
        """
        wanted = query.casefold()
        for entity in candidates:
            if str(entity.get("name", "")).casefold() == wanted:
                return entity
        substring: list[dict[str, Any]] = [
            entity
            for entity in candidates
            if wanted in str(entity.get("name", "")).casefold()
        ]
        if substring:
            return min(substring, key=lambda e: len(str(e.get("name", ""))))
        return candidates[0]

    # -- typed-error cells (evidence in detail; no fabricated data) ----------

    def _no_team(self, sport: str, category: str) -> PrimeSportDataError:
        return NotFound(
            f"{sport} {category} needs a team= param: no verified date-global "
            f"schedule endpoint exists on this sofascore API version (probed "
            f"2026-09-02: /sport/{sport}/scheduled-events/2026-09-02 and "
            f"/sport/{sport}/events/2026-09-02 and finished-events variants "
            f"all returned HTTP 404 on www + api hosts — docs/source-health.md). "
            f"Pass team= to use the verified team-scoped endpoint.",
            source=self.source,
        )

    def _tennis_unavailable(self, category: str) -> PrimeSportDataError:
        return NotFound(
            f"tennis {category} has no verified endpoint on this sofascore API "
            f"version: player event lists 404 (/team/{{playerId}}/events/next/0 "
            f"probed 2026-09-02), date-global schedule paths 404, and search "
            f"results for tennis players carry no sport marker for entity "
            f"resolution. Tennis live (/sport/tennis/events/live) works — "
            f"docs/source-health.md.",
            source=self.source,
        )

    def _h2h_unavailable(self) -> PrimeSportDataError:
        return NotFound(
            "sofascore does not expose head-to-head meeting lists without a "
            "session token (probed 2026-09-02: /event/{id}/h2h/events -> HTTP "
            "404 on www + api hosts; /event/{id}/h2h -> HTTP 200 but only an "
            "aggregate {teamDuel, managerDuel} summary with no events or side "
            "names — insufficient for the h2h envelope). Evidence: "
            "docs/source-health.md and "
            "tests/fixtures/sofascore/h2h/event_duel_aggregate.json.",
            source=self.source,
        )

    # -- normalization ------------------------------------------------------

    def _normalize_event(self, raw: dict[str, Any]) -> Event:
        status = raw.get("status")
        status_type = status.get("type") if isinstance(status, dict) else None
        if not isinstance(status_type, str):
            raise _SkipEvent("event missing status.type")
        mapped = _STATUS_MAP.get(status_type)
        if mapped is None:
            raise _SkipEvent(f"unknown status type {status_type!r}")
        sport_slug = (((raw.get("tournament") or {}).get("category") or {}).get("sport") or {}).get(
            "slug"
        )
        if sport_slug not in _SPORTS:
            raise _SkipEvent(f"unknown sport slug {sport_slug!r}")
        live = status_type == "inprogress"
        raw_home_score = raw.get("homeScore")
        home_score: dict[str, Any] = raw_home_score if isinstance(raw_home_score, dict) else {}
        raw_away_score = raw.get("awayScore")
        away_score: dict[str, Any] = raw_away_score if isinstance(raw_away_score, dict) else {}
        home = self._team_from(
            raw.get("homeTeam"), score=_int_or_none(home_score.get("current")), live=live
        )
        away = self._team_from(
            raw.get("awayTeam"), score=_int_or_none(away_score.get("current")), live=live
        )
        if home is None or away is None:
            raise _SkipEvent("event missing a competitor name")
        event_id = raw.get("id")
        if not isinstance(event_id, int):
            raise _SkipEvent("event missing numeric id")
        lines = self._score_lines(sport_slug, status_type, home_score, away_score)
        return Event(
            sport=sport_slug,
            external=ExternalRef(source=self.source, source_event_id=str(event_id)),
            start_time_utc=_start_time_utc(raw.get("startTimestamp")),
            status=mapped,
            home=home,
            away=away,
            score_lines=lines,
            competition=_competition(raw.get("tournament")),
            live_detail=_live_detail(raw, status_type),
            round_label=_round_label(raw.get("roundInfo")),
        )

    @staticmethod
    def _team_from(competitor: Any, *, score: int | None, live: bool) -> Team | None:
        if not isinstance(competitor, dict):
            return None
        name = competitor.get("name") or competitor.get("shortName")
        if not isinstance(name, str) or not name:
            return None
        team_id = competitor.get("id")
        return Team(
            name=name,
            source_id=team_id if isinstance(team_id, int) else None,
            score=score,
            current_score=live,
        )

    @staticmethod
    def _score_lines(
        sport: str, status_type: str, home_score: Any, away_score: Any
    ) -> list[ScoreLine]:
        """Period/set lines from the event's score objects (SPEC labels).

        Football: H1/H2 from period1/period2, plus FT when finished (period2
        is absent while the first half is still in play — observed 2026-09-02).
        Basketball: Q1..Q4 from period1..period4, OT from an overtime key
        (overtime not present in the recorded fixtures; guarded, not assumed
        beyond a single label). Tennis: S1.. from period1..N (games per set).
        """
        home_score = home_score if isinstance(home_score, dict) else {}
        away_score = away_score if isinstance(away_score, dict) else {}
        lines: list[ScoreLine] = []
        if sport == _TENNIS:
            n = 1
            while f"period{n}" in home_score or f"period{n}" in away_score:
                lines.append(
                    ScoreLine(
                        period_label=f"S{n}",
                        home=_int_or_none(home_score.get(f"period{n}")),
                        away=_int_or_none(away_score.get(f"period{n}")),
                    )
                )
                n += 1
            return lines
        if sport == _FOOTBALL:
            for label, key in (("H1", "period1"), ("H2", "period2")):
                if key in home_score or key in away_score:
                    lines.append(
                        ScoreLine(
                            period_label=label,
                            home=_int_or_none(home_score.get(key)),
                            away=_int_or_none(away_score.get(key)),
                        )
                    )
            if status_type == "finished":
                lines.append(
                    ScoreLine(
                        period_label="FT",
                        home=_int_or_none(home_score.get("current")),
                        away=_int_or_none(away_score.get("current")),
                    )
                )
            return lines
        for n in range(1, 5):
            key = f"period{n}"
            if key in home_score or key in away_score:
                lines.append(
                    ScoreLine(
                        period_label=f"Q{n}",
                        home=_int_or_none(home_score.get(key)),
                        away=_int_or_none(away_score.get(key)),
                    )
                )
        if "overtime" in home_score or "overtime" in away_score:
            lines.append(
                ScoreLine(
                    period_label="OT",
                    home=_int_or_none(home_score.get("overtime")),
                    away=_int_or_none(away_score.get("overtime")),
                )
            )
        return lines


class _SkipEvent(Exception):
    """Internal: this raw event cannot be normalized honestly; skip + warn."""


def _shared_upcoming_event(
    list_a: list[Any],
    list_b: list[Any],
    id_a: int,
    id_b: int,
) -> dict[str, Any] | None:
    """The earliest common fixture of the two team-event pages.

    An event matches when one side carries ``id_a`` and the other ``id_b``
    (home/away either way).  Only scheduled/live fixtures are considered —
    a finished meeting is not a pre-match lineup target.  Ties prefer the
    earlier start timestamp; equal timestamps keep page order.
    """
    upcoming_statuses = {"notstarted", "inprogress"}
    by_id: dict[Any, dict[str, Any]] = {}
    for page, other_id in ((list_a, id_b), (list_b, id_a)):
        for raw in page:
            if not isinstance(raw, dict):
                continue
            event_id = raw.get("id")
            if not isinstance(event_id, int):
                continue
            status = raw.get("status")
            if not isinstance(status, dict) or status.get("type") not in upcoming_statuses:
                continue
            home = raw.get("homeTeam")
            away = raw.get("awayTeam")
            if not isinstance(home, dict) or not isinstance(away, dict):
                continue
            side_ids = {home.get("id"), away.get("id")}
            if id_a not in side_ids or other_id not in side_ids:
                continue
            if event_id not in by_id or _event_start(raw) < _event_start(by_id[event_id]):
                by_id[event_id] = raw
    if not by_id:
        return None
    return min(by_id.values(), key=_event_start)


def _event_start(raw: dict[str, Any]) -> float:
    stamp = raw.get("startTimestamp")
    return float(stamp) if isinstance(stamp, (int, float)) else float("inf")


def _normalize_lineups_document(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the raw ``/event/{id}/lineups`` document (probed 2026-09-20).

    Keeps identity fields only: player name/position/jersey/substitute and
    the source's missing-players list (injuries/suspensions).  Per-player
    statistics and support staff are deliberately dropped.
    """
    confirmed = raw.get("confirmed") is True
    return {
        "confirmed": confirmed,
        "home": _normalize_team_sheet(raw.get("home")),
        "away": _normalize_team_sheet(raw.get("away")),
    }


def _normalize_team_sheet(side: Any) -> dict[str, Any]:
    if not isinstance(side, dict):
        return {"formation": None, "coach": None, "players": [], "missing_players": []}
    formation = side.get("formation")
    coach_raw = side.get("coach")
    coach = None
    if isinstance(coach_raw, dict) and isinstance(coach_raw.get("name"), str):
        coach = coach_raw["name"]
    players: list[dict[str, Any]] = []
    for entry in side.get("players") or []:
        if not isinstance(entry, dict):
            continue
        player = entry.get("player")
        if not isinstance(player, dict) or not isinstance(player.get("name"), str):
            continue
        players.append(
            {
                "name": player["name"],
                "position": player.get("position"),
                "jersey_number": entry.get("jerseyNumber")
                if isinstance(entry.get("jerseyNumber"), int)
                else None,
                "substitute": entry.get("substitute") is True,
            }
        )
    missing: list[str] = []
    for entry in side.get("missingPlayers") or []:
        if not isinstance(entry, dict):
            continue
        player = entry.get("player")
        if isinstance(player, dict) and isinstance(player.get("name"), str):
            missing.append(player["name"])
    return {
        "formation": formation if isinstance(formation, str) else None,
        "coach": coach,
        "players": players,
        "missing_players": missing,
    }


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _start_time_utc(timestamp: Any) -> str | None:
    if not isinstance(timestamp, int):
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _competition(tournament: Any) -> Competition | None:
    if not isinstance(tournament, dict):
        return None
    unique = tournament.get("uniqueTournament")
    name = None
    if isinstance(unique, dict) and isinstance(unique.get("name"), str) and unique.get("name"):
        name = unique["name"]
    elif isinstance(tournament.get("name"), str) and tournament.get("name"):
        name = tournament["name"]
    category = tournament.get("category")
    country = None
    if isinstance(category, dict):
        c_country = category.get("country")
        if isinstance(c_country, dict) and isinstance(c_country.get("name"), str):
            country = c_country["name"]
    if name is None:
        return None
    return Competition(name=name, country=country)


def _live_detail(raw: dict[str, Any], status_type: str) -> str | None:
    if status_type != "inprogress":
        return None
    status = raw.get("status")
    if isinstance(status, dict) and isinstance(status.get("description"), str):
        return status["description"]
    return None


def _round_label(round_info: Any) -> str | None:
    if not isinstance(round_info, dict):
        return None
    name = round_info.get("name")
    if isinstance(name, str) and name:
        return name
    number = round_info.get("round")
    if isinstance(number, int):
        return f"Round {number}"
    return None


__all__ = ["SOFASCORE_IPV4_FALLBACKS", "USER_AGENT", "SofascoreAdapter", "ipv4_only"]
