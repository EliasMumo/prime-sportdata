"""Livescore adapter — graceful-blocked, probe-driven (2026-09-02).

See the "Livescore (Stage 4b, 2026-09-02)" section of
``docs/source-health.md`` for the probe log. Honest summary:
``www.livescore.com`` has never answered a single HTTP request from this
network. The SPEC evidence row and this build's re-probe (2026-09-02, two
sequential attempts) both ended in TCP connect timeouts — the second attempt
forced IPv4 (``curl -4``) and still timed out — while sibling sports hosts
answered normally. The edge blocks this client.

Consequences for this adapter (SPEC: never fabricate, never parse unverified
shapes):

* NO data path ships. The only URL ever requested is the site homepage
  (``https://www.livescore.com/en/``), which is not a data endpoint, and no
  livescore response was ever recorded — no content shape exists to parse, so
  ``parse_events`` raises a typed error on any input.
* ``fetch()`` is fully structured per the base contract and performs the real
  request, so a future session whose network behaves differently is observed
  and mapped instead of guessed: transport timeouts/refusals (the observed
  reality this build) raise ``SourceUnavailable`` whose detail quotes the
  probe evidence; HTTP 403 and bot-challenge pages raise ``SourceBlocked``;
  HTTP 429 raises ``RateLimited``; HTTP 5xx (retries exhausted) raises
  ``SourceUnavailable``. An unexpected HTTP 200 passes through as a raw
  ``SourceResponse`` and ``parse_events`` then raises ``NoData`` — enabling a
  livescore data path requires a fresh probe that records a real data
  response AND a real parse contract first, never a guess from the homepage.
* All 12 catalog cells behave identically (single probed URL): the observed
  outcome is the same typed error regardless of sport/category, and query
  params cannot shape a request to an endpoint that was never verified to
  exist. Unknown sports/categories raise ``BadRequest`` before any request.
* Retry discipline follows the SPEC cap (max 2 retries, exponential backoff
  with jitter, ONLY on transport errors and HTTP 5xx — never on 403/429).
  One static realistic browser UA, no rotation. Short connect timeout (8 s):
  this host TCP-times-out, so longer waits would only burn the shared
  residential IP budget.

Type-mapping table (module-level ``_map_http_outcome``, unit-tested offline):

    observed reality  transport timeout/refused -> SourceUnavailable
    future behavior   HTTP 403 / challenge page -> SourceBlocked
    future behavior   HTTP 429                  -> RateLimited
    future behavior   HTTP 5xx (retries done)   -> SourceUnavailable
    future behavior   HTTP 200 (non-challenge)  -> raw SourceResponse; parse
                                                   raises NoData (no contract)
    any other status                            -> SourceUnavailable
"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import ClassVar

import httpx

from prime_sportdata.errors import (
    BadRequest,
    NoData,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.sources.base import ParseOutcome, SourceAdapter, SourceResponse

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

# The only URL ever requested for this source — the site homepage, probed in
# the SPEC evidence session and re-probed this build. It is NOT a data
# endpoint; it exists here only as the URL whose observed failure maps to the
# typed error the adapter ships.
BASE_URL = "https://www.livescore.com/en/"

CONNECT_TIMEOUT_S = 8.0
MAX_RETRIES = 2
_BACKOFF_BASE_S = 1.5
_JITTER_MAX_S = 0.8

_SPORTS: tuple[str, ...] = ("football", "tennis", "basketball")
_CATEGORIES: tuple[str, ...] = ("fixtures", "live", "results", "h2h")

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

    Pure function of the observed response (tested offline). No livescore data
    path exists, so no status here may produce data: 403/429/challenge map to
    their typed errors, and everything else either raises ``SourceUnavailable``
    or passes the raw body through for ``parse_events`` to reject with
    ``NoData`` (no content shape was ever verified).
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
    raise SourceUnavailable(
        f"unexpected HTTP {status} from {url} — no livescore data path is "
        "verified and this status was never observed (docs/source-health.md); "
        "re-probe before enabling",
        source=source,
    )


class LivescoreAdapter(SourceAdapter):
    """Livescore adapter: graceful-blocked, structured per the base contract.

    Implements ``fetch`` + ``parse_events`` but ships NO data path: no
    livescore response was ever recorded on this network (2026-09-02 probe
    evidence in the module docstring and docs/source-health.md), so there is
    nothing verified to parse and no invented rows are ever returned. Every
    valid (sport, category) cell performs the real request against the single
    probed URL and surfaces the typed error the network actually produces
    (this build: ``SourceUnavailable`` quoting the probe evidence).
    """

    source: ClassVar[str] = "livescore"

    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        """Real HTTP request for ``sport/category``; typed errors only.

        Validates inputs, then attempts the one URL that was ever probed. On
        this network that attempt TCP-times-out and raises ``SourceUnavailable``
        with the probe evidence in its detail. ``params`` (date/team/league/
        limit) cannot shape a request to an endpoint that was never verified
        to exist, so they are ignored — documented, not silent.
        """
        sport, category = str(sport), str(category)
        if sport not in _SPORTS:
            raise BadRequest(f"unknown sport {sport!r}", source=self.source)
        if category not in _CATEGORIES:
            raise BadRequest(f"unknown category {category!r}", source=self.source)
        return self._request(BASE_URL)

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        """No livescore content shape was ever verified — always typed error.

        No livescore response was ever recorded on this network (every
        2026-09-02 probe TCP-timed out), so parsing any payload into events
        would require inventing a content contract. Raises ``NoData`` on any
        input, never returns events.
        """
        raise NoData(
            "no livescore parse contract exists: the only URL ever requested "
            f"for this source ({resp.url}) is the site homepage, and no "
            "livescore response was ever recorded on this network (2026-09-02: "
            "TCP connect timeouts, docs/source-health.md); parsing any content "
            "into events would be an invented shape. Re-probe and record a "
            "real data response before any parse ships.",
            source=resp.source,
        ) from None

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
    "BASE_URL",
    "CONNECT_TIMEOUT_S",
    "MAX_RETRIES",
    "USER_AGENT",
    "LivescoreAdapter",
    "_map_http_outcome",
]
