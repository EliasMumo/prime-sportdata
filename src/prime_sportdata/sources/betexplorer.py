"""BetExplorer odds adapter — probe-driven (2026-09-08). See ``docs/source-health.md``.

Only endpoints that were live-fetched AND successfully parsed during the
2026-09-08 probe session are shipped as data paths (SPEC: every shipped URL
must have been verified by a live fetch). This adapter is odds-only: the
score adapters (flashscore/sofascore/livescore) keep their own categories.

Network realities recorded that session
---------------------------------------
* ``GET https://www.betexplorer.com/{sport}/`` (football/basketball/tennis)
  answered HTTP 200 over plain ``curl -4`` with a static browser UA, serving
  full server-rendered HTML tables (football ~476 KB, tennis ~309 KB,
  basketball ~153 KB). No challenge page detected.
* Match rows carry ``data-dt="D,M,YYYY,H,MM"`` (e.g. ``8,9,2026,9,45``),
  rendered in the site's server timezone. The site is Prague-based and the
  probed dates matched Europe/Prague local days, so kickoffs are parsed as
  Europe/Prague and converted to UTC with a standing warning (timezone
  assumption, never silent).
* Each match row links ``<a href="/{sport}/.../slug/{EVENT_ID}/">Home - Away</a>``;
  the last path segment is the stable source event id (also present as
  ``matchid=`` in the row's ``my_selections_click`` payloads).
* Odds columns: football rows carry three ``data-odd`` prices (market ``1x2``:
  1/X/2); basketball and tennis rows carry two (market ``home_away``: home/
  away, ``my_selections_click('ha', ...)``). The column is BetExplorer's own
  default odds display — one price per selection, not a named execution
  bookmaker — so quotes record ``bookmaker="betexplorer-default"`` with a
  standing warning.
* League context comes from the preceding ``<tr class="js-tournament">``
  header row (``<a class="table-main__tournament">Country: League</a>``).

Retry discipline (SPEC Ban-risk policy): max 2 retries, exponential backoff
with jitter, ONLY on transport timeouts/errors and HTTP 5xx — never on
403/429 (those map straight to ``SourceBlocked``/``RateLimited`` and trip the
engine breaker). One static realistic browser UA, no rotation, single host
(``www.betexplorer.com``), sequential requests >= 1 s apart via the engine
rate limiter.
"""

from __future__ import annotations

import random
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from html import unescape
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from prime_sportdata.errors import (
    BadRequest,
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.betexplorer.com/",
}

BASE_URL = "https://www.betexplorer.com"
_BETEXPLORER_HOST = "www.betexplorer.com"

CONNECT_TIMEOUT_S = 15.0
MAX_RETRIES = 2
_BACKOFF_BASE_S = 1.5
_JITTER_MAX_S = 0.8

_SPORTS = ("football", "basketball", "tennis")
_ODDS_CATEGORY = "odds"
# Number of odds columns per sport on the verified main page: football = 1x2,
# basketball/tennis = two-way home/away (verified 2026-09-08, see docstring).
_MARKET_BY_SPORT: dict[str, tuple[str, tuple[str, ...]]] = {
    "football": ("1x2", ("1", "X", "2")),
    "basketball": ("home_away", ("home", "away")),
    "tennis": ("home_away", ("home", "away")),
}
_BOOKMAKER_LABEL = "betexplorer-default"

# Prag force-free zone: BetExplorer works with plain IPv4; on this network
# IPv6 toward the host blackholes (same evidence class as sofascore). Pin
# AF_INET for this host only, scoped like the sofascore shim.
_real_getaddrinfo: Any = socket.getaddrinfo
_getaddrinfo_lock = threading.Lock()


def _betexplorer_getaddrinfo(host: str, port: int | str | None, *args: Any, **kwargs: Any) -> list[Any]:
    if host != _BETEXPLORER_HOST:
        return _real_getaddrinfo(host, port, *args, **kwargs)
    if args:
        args = (socket.AF_INET,) + args[1:]
    elif "family" in kwargs:
        kwargs["family"] = socket.AF_INET
    else:
        args = (socket.AF_INET,)
    return _real_getaddrinfo(host, port, *args, **kwargs)


@contextmanager
def ipv4_only() -> Iterator[None]:
    """Scope the AF_INET shim to one request (process-global socket patch)."""
    with _getaddrinfo_lock:
        original = socket.getaddrinfo
        socket.getaddrinfo = _betexplorer_getaddrinfo  # type: ignore[assignment]
        try:
            yield
        finally:
            socket.getaddrinfo = original


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter between retries (SPEC §5)."""
    time.sleep(_BACKOFF_BASE_S * (2**attempt) + random.uniform(0.0, _JITTER_MAX_S))


def _parse_kickoff_utc(data_dt: str) -> str | None:
    """``D,M,YYYY,H,MM`` (BetExplorer server time, Europe/Prague) -> UTC ISO.

    Returns ``None`` when the shape or the tz database fails; callers attach a
    warning instead of guessing.
    """
    try:
        day, month, year, hour, minute = (int(part) for part in data_dt.split(","))
        prague = ZoneInfo("Europe/Prague")
        local = datetime(year, month, day, hour, minute, tzinfo=prague)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return local.astimezone(UTC).isoformat()


def _market_for_sport(sport: str) -> tuple[str, tuple[str, ...]]:
    try:
        return _MARKET_BY_SPORT[sport]
    except KeyError:
        raise BadRequest(f"unknown sport {sport!r}", source="betexplorer") from None


class BetexplorerAdapter(SourceAdapter):
    """BetExplorer odds adapter: plain-httpx IPv4 client, static UA, typed errors."""

    source: ClassVar[str] = "betexplorer"

    # -- contract -----------------------------------------------------------

    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        sport, category = str(sport), str(category)
        if sport not in _SPORTS:
            raise BadRequest(f"unknown sport {sport!r}", source=self.source)
        if category != _ODDS_CATEGORY:
            raise NotFound(
                f"betexplorer ships an odds-only data path; no verified {category!r} "
                f"endpoint (sport={sport})",
                source=self.source,
            )
        # The verified page is the site's own "next matches today" list.  A
        # requested past/future date has no verified URL shape, so it fails
        # honestly instead of returning rows for the wrong day.
        requested = (params.get("date") or "").strip()
        if requested:
            try:
                prague = ZoneInfo("Europe/Prague")
            except ZoneInfoNotFoundError:
                prague = None
            today = datetime.now(prague if prague is not None else UTC).date().isoformat()
            if requested != today:
                raise NotFound(
                    f"betexplorer date filtering is unverified; requested {requested} "
                    f"but the shipped page serves {today} (Europe/Prague)",
                    source=self.source,
                )
        return self._request(f"{BASE_URL}/{sport}/")

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        raise NotFound(
            "betexplorer has no verified fixtures/results data path (odds-only adapter)",
            source=resp.source,
        )

    def parse_odds(self, resp: SourceResponse) -> ParseOutcome:
        """Parse the server-rendered odds table into normalized quotes."""
        if not isinstance(resp.payload, (str, bytes)):
            raise NoData(
                f"betexplorer payload must be HTML text, got "
                f"{type(resp.payload).__name__}",
                source=resp.source,
            )
        html = resp.payload.decode("utf-8", errors="replace") if isinstance(resp.payload, bytes) else resp.payload
        sport = self._sport_from_url(resp.url)
        market, selections = _market_for_sport(sport)
        warnings: list[str] = [
            parse_warning(
                "kickoff times parsed as Europe/Prague (BetExplorer server timezone assumption)",
                resp,
            ),
            parse_warning(
                "odds column is the site's default odds display, not a named execution "
                "bookmaker — quotes are observed market evidence only",
                resp,
            ),
        ]
        quotes: list[OddsQuote] = []
        skipped: dict[str, int] = {}
        competition: Competition | None = None
        for row in _iter_rows(html):
            kind = _row_kind(row)
            if kind == "tournament":
                competition = _parse_tournament(row) or competition
                continue
            if kind != "match":
                continue
            try:
                quote = self._normalize_row(row, sport, market, selections, competition, resp)
            except _SkipRow as exc:
                reason = str(exc)
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            if quote is not None:
                quotes.append(quote)
        for reason, count in sorted(skipped.items()):
            warnings.append(parse_warning(f"skipped {count} row(s): {reason}", resp))
        return ParseOutcome(quotes=quotes, warnings=warnings)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _sport_from_url(url: str) -> str:
        for sport in _SPORTS:
            if f"/{sport}/" in url:
                return sport
        raise NoData(f"cannot derive sport from URL {url}", source="betexplorer")

    def _normalize_row(
        self,
        row: str,
        sport: str,
        market: str,
        selections: tuple[str, ...],
        competition: Competition | None,
        resp: SourceResponse,
    ) -> OddsQuote | None:
        data_dt = _attr(row, "data-dt")
        teams_link = _match_link(row, sport)
        if teams_link is None:
            raise _SkipRow("no team link")
        event_id, label = teams_link
        if not event_id or " - " not in label:
            raise _SkipRow("unparseable team link")
        home, _, away = label.partition(" - ")
        if not home.strip() or not away.strip():
            raise _SkipRow("empty team name")
        prices = _odds_prices(row)
        if len(prices) != len(selections):
            raise _SkipRow(
                f"expected {len(selections)} odds for {market}, found {len(prices)}"
            )
        parsed: dict[str, float] = {}
        for selection, raw in zip(selections, prices):
            try:
                value = float(raw)
            except ValueError:
                raise _SkipRow(f"non-numeric odds {raw!r}") from None
            if not value > 1.0:
                raise _SkipRow(f"odds {value} is not a valid decimal > 1")
            parsed[selection] = value
        kickoff = data_dt.strip() if data_dt else None
        start_time_utc = _parse_kickoff_utc(kickoff) if kickoff else None
        return OddsQuote(
            sport=sport,  # type: ignore[arg-type]
            external=ExternalRef(
                source=self.source,
                source_event_id=event_id,
                source_url=f"{BASE_URL}/{sport}/",
            ),
            start_time_utc=start_time_utc,
            home=unescape(home.strip()),
            away=unescape(away.strip()),
            competition=competition,
            market=market,
            bookmaker=_BOOKMAKER_LABEL,
            prices=parsed,
        )

    # -- request ------------------------------------------------------------

    def _request(self, url: str) -> SourceResponse:
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
            if resp.status_code == 200:
                return SourceResponse(
                    source=self.source,
                    payload=resp.content,
                    url=url,
                    status=200,
                    fetched_at=datetime.now(UTC).isoformat(),
                )
            if resp.status_code == 403:
                raise SourceBlocked(f"HTTP 403 from {url}", source=self.source)
            if resp.status_code == 429:
                raise RateLimited(f"HTTP 429 from {url}", source=self.source)
            if resp.status_code == 404:
                raise NotFound(f"HTTP 404 — path not on this site version: {url}", source=self.source)
            raise SourceUnavailable(f"unexpected HTTP {resp.status_code} from {url}", source=self.source)


# --- HTML row helpers (stdlib only; no new dependencies) ----------------------


class _SkipRow(Exception):
    """One unusable row; counted into parse warnings, never guessed."""


def _iter_rows(html: str) -> Iterator[str]:
    """Yield ``<tr ...>...</tr>`` blocks. BetExplorer match tables are flat
    (rows never nest), verified against the live payloads."""
    import re

    for match in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", html, flags=re.DOTALL | re.IGNORECASE):
        yield match.group(0)


def _row_kind(row: str) -> str | None:
    if "js-tournament" in row or "table-main__tournament" in row:
        return "tournament"
    if "data-dt=" in row:
        return "match"
    return None


def _attr(row: str, name: str) -> str | None:
    import re

    match = re.search(rf"{name}=[\"']([^\"']*)[\"']", row)
    return match.group(1) if match else None


def _match_link(row: str, sport: str) -> tuple[str, str] | None:
    """Return (event_id, link text) for the row's match anchor."""
    import re

    pattern = rf"<a\s[^>]*href=[\"']/{sport}/[^\"']*/([^/\"']+)/[\"'][^>]*>(.*?)</a>"
    match = re.search(pattern, row, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    event_id, label = match.group(1), re.sub(r"<[^>]+>", "", match.group(2))
    return event_id, unescape(label.strip())


def _odds_prices(row: str) -> list[str]:
    import re

    return re.findall(r"data-odd=[\"']([^\"']+)[\"']", row)


def _parse_tournament(row: str) -> Competition | None:
    """``<a class="table-main__tournament">Country: League</a>`` -> Competition."""
    import re

    match = re.search(
        r'table-main__tournament[^>]*>(.*?)</a>',
        row,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None
    text = unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
    if not text:
        return None
    country, sep, name = text.partition(": ")
    if sep:
        return Competition(name=name.strip(), country=country.strip())
    return Competition(name=text)
