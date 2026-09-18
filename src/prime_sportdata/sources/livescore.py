"""Livescore adapter — verified date API for fixtures/results (2026-09-18).

``www.livescore.com`` has never answered a request from this network, but
``prod-public-api.livescore.com`` does.  Probe (2026-09-18):

    GET https://prod-public-api.livescore.com/v1/api/app/date/soccer/20260916/1.50
    -> HTTP 200, Stages[].Events[] with names (T1/T2[0].Nm), status (Eps:
    NS scheduled, FT finished, HT half-time, Postp. postponed), full-time
    scores (Tr1/Tr2), half-time scores (Trh1/Trh2) and a numeric start
    timestamp (Esd, YYYYMMDDHHMMSS).  Same shape for basketball/tennis
    slugs.

Consequences for this adapter (never fabricate, never parse unverified
shapes):

* fixtures/results use the date endpoint above; the response carries both
  scheduled and finished events (callers filter by status).
* live/h2h still ship no data path (the only previously probed URL, the
  homepage, remains TCP-blocked on this network) and surface the probe
  evidence as a typed error.
* ``Esd`` carries no timezone; it is emitted verbatim as a naive ISO
  string and every parse attaches a standing warning.
"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx

from prime_sportdata.errors import (
    BadRequest,
    NoData,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.models import (
    Competition,
    Event,
    EventStatus,
    ExternalRef,
    ScoreLine,
    Team,
)
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# The only URL ever requested for the live/h2h cells — the site homepage,
# probed in the SPEC evidence session and re-probed this build. It is NOT a
# data endpoint.
BASE_URL = "https://www.livescore.com/en/"

# Verified 2026-09-18: date endpoint with full fixtures/results data.
API_BASE = "https://prod-public-api.livescore.com/v1/api/app/date"
API_VERSION = "1.50"

CONNECT_TIMEOUT_S = 8.0
MAX_RETRIES = 2
_BACKOFF_BASE_S = 1.5
_JITTER_MAX_S = 0.8

_SPORTS: tuple[str, ...] = ("football", "tennis", "basketball")
_SPORT_SLUGS: dict[str, str] = {
    "football": "soccer",
    "tennis": "tennis",
    "basketball": "basketball",
}
_CATEGORIES: tuple[str, ...] = ("fixtures", "live", "results", "h2h")

# livescore Eps -> SPEC EventStatus (observed 2026-09-18). Unknown values are
# skipped with a warning, never guessed.
_STATUS_MAP: dict[str, EventStatus] = {
    "NS": "scheduled",
    "FT": "finished",
    "HT": "live",
    "Postp.": "postponed",
    "AP": "postponed",
    "CAN": "cancelled",
}

# Probe evidence quoted verbatim in typed-error details (2026-09-02; this
# build's own re-probe plus the SPEC network table row).
_PROBE_EVIDENCE = (
    "www.livescore.com/en/ TCP connect timed out on every 2026-09-02 attempt: "
    "this build's probe saw 2 consecutive connect timeouts after ~8 s (plain "
    "curl and curl -4 forced IPv4, static Chrome UA; http=000, no connection "
    "established) and the SPEC evidence row recorded the same over curl -4 — "
    "the edge blocks this client (docs/source-health.md)"
)

# Bot-challenge signatures in a 200 body -> SourceBlocked (never guessed from
# the bare HTML of the homepage, which a healthy livescore.com would also
# serve: a plain 200 without these markers is an unverified-content NoData).
_CHALLENGE_TOKENS: tuple[bytes, ...] = (
    b"cloudflare",
    b"cf-challenge",
    b"challenge-platform",
    b"captcha",
    b"just a moment",
    b"checking your browser",
    b"access denied",
    b"attention required",
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter between retries (SPEC §5)."""
    time.sleep(_BACKOFF_BASE_S * (2**attempt) + random.uniform(0.0, _JITTER_MAX_S))


def _looks_like_challenge(body: bytes) -> bool:
    head = body[:4096].lower()
    return any(token in head for token in _CHALLENGE_TOKENS)


def _map_http_outcome(
    url: str, status: int, body: bytes, fetched_at: str, source: str
) -> SourceResponse:
    """Turn one final HTTP outcome into a response or a typed error.

    Pure function of the observed response (tested offline): 403/429/
    challenge pages map to their typed errors; a 200 passes through as raw
    bytes (date-API parsing happens in ``parse_events``).
    """
    if status == 403:
        raise SourceBlocked(f"HTTP 403 from {url}", source=source)
    if status == 429:
        raise RateLimited(f"HTTP 429 from {url}", source=source)
    if status == 200:
        if _looks_like_challenge(body):
            raise SourceBlocked(
                f"HTTP 200 served a bot-challenge page (challenge signature in "
                f"body) at {url}",
                source=source,
            )
        return SourceResponse(source=source, payload=body, url=url, status=200, fetched_at=fetched_at)
    if status >= 500:
        raise SourceUnavailable(f"HTTP {status} (retries exhausted) from {url}", source=source)
    raise SourceUnavailable(f"unexpected HTTP {status} from {url}", source=source)


class LivescoreAdapter(SourceAdapter):
    """Livescore adapter: date API for fixtures/results, homepage for the rest.

    ``fetch`` validates the (sport, category) cell and shapes the verified
    date endpoint for fixtures/results; live/h2h keep the single previously
    probed URL whose observed network outcome (this network: TCP timeouts)
    surfaces as a typed error.
    """

    source: ClassVar[str] = "livescore"

    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        """Real HTTP request for ``sport/category``; typed errors only."""
        sport, category = str(sport), str(category)
        if sport not in _SPORTS:
            raise BadRequest(f"unknown sport {sport!r}", source=self.source)
        if category not in _CATEGORIES:
            raise BadRequest(f"unknown category {category!r}", source=self.source)
        if category in {"fixtures", "results"}:
            raw_date = (params.get("date") or "").strip()
            try:
                compact = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%Y%m%d")  # noqa: DTZ007
            except ValueError:
                raise BadRequest(
                    f"livescore date must be YYYY-MM-DD, got {raw_date!r}",
                    source=self.source,
                ) from None
            return self._request(
                f"{API_BASE}/{_SPORT_SLUGS[sport]}/{compact}/{API_VERSION}"
            )
        return self._request(BASE_URL)

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        """Parse the verified date-API shape; anything else stays NoData."""
        if "/date/" not in resp.url:
            raise NoData(
                "no livescore parse contract exists for this URL: only the "
                f"verified date endpoint carries data; {resp.url} is not it",
                source=resp.source,
            ) from None
        data = parse_json_response(resp)
        stages = data.get("Stages") if isinstance(data, dict) else None
        if not isinstance(stages, list):
            raise NoData(
                f"livescore payload lacks a Stages[] list (top-level keys: "
                f"{sorted(data) if isinstance(data, dict) else type(data).__name__})",
                source=resp.source,
            )
        warnings = [
            parse_warning(
                "livescore Esd carries no timezone; emitted verbatim as a "
                "naive ISO timestamp (source offset unverified)",
                resp,
            )
        ]
        sport = "football"
        try:
            url_parts = resp.url.split("/")
            slug = url_parts[url_parts.index("date") + 1]
            sport = next(
                (name for name, s in _SPORT_SLUGS.items() if s == slug), "football"
            )
        except (ValueError, IndexError):
            sport = "football"
        events: list[Event] = []
        skipped: dict[str, int] = {}
        for stage in stages:
            if not isinstance(stage, dict):
                skipped["stage entry is not an object"] = skipped.get("stage entry is not an object", 0) + 1
                continue
            stage_name = str(stage.get("Snm") or "")
            for raw in stage.get("Events") or []:
                if not isinstance(raw, dict):
                    skipped["event entry is not an object"] = skipped.get("event entry is not an object", 0) + 1
                    continue
                event = self._normalize_event(raw, stage_name, sport)
                if event is None:
                    skipped["unknown status or missing team"] = skipped.get("unknown status or missing team", 0) + 1
                    continue
                events.append(event)
        for reason, count in sorted(skipped.items()):
            warnings.append(parse_warning(f"skipped {count} event(s): {reason}", resp))
        return ParseOutcome(events=stamp_provenance(resp, events), warnings=warnings)

    @staticmethod
    def _normalize_event(raw: dict[str, object], stage_name: str, sport: Any) -> Event | None:
        """One livescore event row -> Event; None for unsupported statuses."""
        status = _STATUS_MAP.get(str(raw.get("Eps") or ""))
        if status is None:
            return None
        home = LivescoreAdapter._team(raw.get("T1"))
        away = LivescoreAdapter._team(raw.get("T2"))
        if home is None or away is None:
            return None
        event_id = raw.get("Eid")
        source_event_id = str(event_id) if event_id is not None else ""
        if not source_event_id:
            return None
        home_score = LivescoreAdapter._score(raw.get("Tr1"))
        away_score = LivescoreAdapter._score(raw.get("Tr2"))
        half_home = LivescoreAdapter._score(raw.get("Trh1"))
        half_away = LivescoreAdapter._score(raw.get("Trh2"))
        score_lines: list[ScoreLine] = []
        if half_home is not None or half_away is not None:
            score_lines.append(ScoreLine(period_label="H1", home=half_home, away=half_away))
        if home_score is not None or away_score is not None:
            score_lines.append(ScoreLine(period_label="FT", home=home_score, away=away_score))
        live = status == "live"
        home_team = Team(name=home, score=home_score, current_score=live)
        away_team = Team(name=away, score=away_score, current_score=live)
        return Event(
            sport=sport,
            external=ExternalRef(source="livescore", source_event_id=source_event_id),
            start_time_utc=LivescoreAdapter._start_time(raw.get("Esd")),
            status=status,
            home=home_team,
            away=away_team,
            score_lines=score_lines,
            competition=Competition(name=stage_name) if stage_name else None,
        )

    @staticmethod
    def _team(value: object) -> str | None:
        if not isinstance(value, list) or not value or not isinstance(value[0], dict):
            return None
        name = value[0].get("Nm")
        if not isinstance(name, str) or not name.strip():
            return None
        return name.strip()

    @staticmethod
    def _score(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _start_time(value: object) -> str | None:
        """Esd (YYYYMMDDHHMMSS int/str) -> naive ISO string, verbatim."""
        raw = str(value or "").strip()
        try:
            parsed = datetime.strptime(raw, "%Y%m%d%H%M%S")  # noqa: DTZ007
        except ValueError:
            return None
        return parsed.isoformat()

    def _request(self, url: str) -> SourceResponse:
        """GET ``url`` with retry discipline; returns or raises a typed error."""
        attempt = 0
        while True:
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(CONNECT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                    headers=_HEADERS,
                    follow_redirects=False,
                ) as client:
                    resp = client.get(url)
            except httpx.TransportError as exc:
                if attempt < MAX_RETRIES:
                    _sleep_backoff(attempt)
                    attempt += 1
                    continue
                raise SourceUnavailable(
                    f"{_PROBE_EVIDENCE}; transport failure after "
                    f"{attempt + 1} attempt(s): {exc} — {url}",
                    source=self.source,
                ) from exc
            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                _sleep_backoff(attempt)
                attempt += 1
                continue
            return _map_http_outcome(url, resp.status_code, resp.content, _utc_now_iso(), self.source)


__all__ = [
    "API_BASE",
    "API_VERSION",
    "BASE_URL",
    "CONNECT_TIMEOUT_S",
    "MAX_RETRIES",
    "USER_AGENT",
    "LivescoreAdapter",
    "_map_http_outcome",
]
