"""Flashscore adapter — probe-driven (2026-09-02). See ``docs/source-health.md``.

Only endpoints that were live-fetched AND successfully parsed during the
2026-09-02 probe session are shipped as data paths (SPEC: every shipped URL
must have been verified by a live fetch). Cells without a verified endpoint
raise typed errors with the evidence in the detail message — never fabricated
rows, never a parse of an unverified shape.

Network realities recorded that session
---------------------------------------
* ``www.flashscore.com/football/`` serves an app-shell HTML (HTTP 200, ~737 KB,
  no server-rendered rows, no embedded JSON) — the data path is a separate
  XHR feed, discovered from the site's own JS chunks on
  ``static.flashscore.com`` (feed client class + inline config in
  ``core_2_2314000000.js``): ``x-fsign: SW9D1eZo``, project id ``2``
  (Flashscore.com), feed resolver ``https://2.flashscore.ninja``.
* Verified data path: ``GET https://2.flashscore.ninja/2/x/feed/<feed>`` with
  the lowercase ``x-fsign`` header answers HTTP 200 text/plain over plain
  httpx (no TLS-fingerprint wall). Response = pipe format: field sep ``÷``,
  pair sep ``¬``, row sep ``~``.
* Day-table feeds (verified 200 this session): ``f_<sportId>_<offset>_3_en_2``
  with sportId football=1 / tennis=2 / basketball=3 and day offset relative to
  the server's Prague (CEST, UTC+2) date. Verified offsets: -1 (yesterday),
  0 (today), +1 (tomorrow) — offsets beyond are raised as ``NotFound`` with
  evidence until probed. A day doc contains scheduled + live + finished +
  postponed rows mixed; the feed carries no category or live-clock semantics
  (the ``AC`` field is NOT the live minute — empirically disproven vs real
  elapsed time and cross-source kickoff evidence, see docs/source-health.md),
  so events keep truthful statuses and parses carry a standing warning that
  category/date filtering is caller-side (same precedent as the sofascore
  adapter's team-scoped warning).
* Status semantics verified empirically: ``AB=1`` scheduled (no score keys),
  ``AB=2`` live (``AG``/``AH`` = current score; tennis: sets won — verified on
  live rows with completed sets), ``AB=3`` + ``AG``/``AH`` ints = finished,
  ``AB=3`` without ``AG`` and ``AC=4`` = postponed (observed football +
  basketball). Unhandled end-state codes (tennis ``AB=3`` no-AG rows with
  ``AC`` in {5, 9}, one carrying a "withdrawn" note) are skipped with a
  counted warning, never guessed into a SPEC status.
* Football: NO period score_lines are shipped. The ``BC``/``BD`` pair on
  football rows is not the half-time score: the Sassuolo–Frosinone match
  (Coppa Italia, kickoff 2026-09-02T13:00:00Z, cross-verified in the sofascore
  live record with H1 1:1) carries ``BC:BD = 0:0`` in the flashscore day feed.
* Basketball: finished rows carry quarter pairs ``BA:BB..BG:BH`` = Q1..Q4 —
  arithmetic sums match ``AG``/``AH`` on all 7 finished rows recorded
  (e.g. 21+22+27+6=76). Quarter lines are only shipped when all four pairs
  are integers AND sum to the final score (an overtime game would break the
  sum; none observed).
* Tennis: ``BA:BB..BI:BJ`` = per-set games (S1..S5); ``AG``/``AH`` = sets won.
  Live rows include the in-progress set's pair (S1 4:1 in play observed), so
  live parses ship it as an S-line exactly like the sofascore adapter does
  for in-play sets.
* Head-to-head: the per-event feed ``df_hh_<sportId>_<eventId>`` was verified
  live (``df_hh_1_KxrZoe94`` -> 200) but requires a source event id; no
  team-name -> event resolution chain was probed this session, and source ids
  are not shared across sources — the SPEC h2h envelope (two entities +
  meetings) cannot be built honestly, so the h2h cell raises ``NotFound``
  with this evidence.

Retry discipline (SPEC Ban-risk policy): max 2 retries, exponential backoff
with jitter, ONLY on transport timeouts/errors and HTTP 5xx — never on
403/429 (those map straight to ``SourceBlocked``/``RateLimited`` and trip the
engine breaker). One static realistic browser UA, no rotation, single
host (``2.flashscore.ninja``), sequential probes >= 1 s apart.

The scrapling escalation hook (``research`` extra, NOT installed this build)
lives in ``FlashscoreAdapter._escalate_to_scrapling``: the plain-httpx feed
path was verified live and is the shipped mechanism, so the hook is inactive
by design and raises ``DependencyMissing`` when the extra is absent. It is
NOT a block/CAPTCHA bypass — enabling it requires a probe-evidenced fallback
shape first (SPEC).
"""

from __future__ import annotations

import random
import time
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from prime_sportdata.errors import (
    BadRequest,
    DependencyMissing,
    NoData,
    NotFound,
    PrimeSportDataError,
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
    Sport,
    Team,
)
from prime_sportdata.sources.base import (
    ParseOutcome,
    SourceAdapter,
    SourceResponse,
    parse_warning,
    stamp_provenance,
)

# One static realistic browser UA per source (SPEC fingerprint hygiene) — the
# same Chrome 131 string sofascore uses.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.flashscore.com/",
    "x-fsign": "SW9D1eZo",  # feed signature, discovered in core2.js config 2026-09-02
}

# Resolver URL (feed_resolver.local in the site config) + project id 2.
FEED_BASE = "https://2.flashscore.ninja/2/x/feed"

_SPORTS: tuple[Sport, ...] = ("football", "tennis", "basketball")
_CATEGORIES = ("fixtures", "live", "results", "h2h")
_SPORT_IDS: dict[str, int] = {"football": 1, "tennis": 2, "basketball": 3}
_SPORT_BY_ID: dict[str, Sport] = {"1": "football", "2": "tennis", "3": "basketball"}
# Day-feed shape suffix; only day offsets within VERIFIED_DAY_OFFSETS may be
# requested (probed live 2026-09-02: f_<s>_{-1,0,1}_3_en_2 all HTTP 200).
_FEED_TAIL = "3_en_2"
VERIFIED_DAY_OFFSETS: tuple[int, ...] = (-1, 0, 1)
_CONNECT_TIMEOUT_S = 12.0
_MAX_RETRIES = 2
_BACKOFF_BASE_S = 1.5
_JITTER_MAX_S = 0.8
# League-name country sentinels that are not countries (recorded: "WORLD:
# Club Friendly" with ZY "World"); mapped to country=None like the sofascore
# adapter treats non-country-scoped competitions (e.g. UEFA).
_NON_COUNTRY: frozenset[str] = frozenset({"world", "europe", "international"})


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
        raise NotFound(f"HTTP 404 from {url}", source=source)
    if status == 400:
        raise BadRequest(f"HTTP 400 from {url}", source=source)
    if status >= 500:
        raise SourceUnavailable(f"HTTP {status} (retries exhausted) from {url}", source=source)
    raise SourceUnavailable(f"unexpected HTTP {status} from {url}", source=source)


def _prague_tz() -> tzinfo:
    """Server day clock: Europe/Prague (CEST UTC+2 in September)."""
    try:
        return ZoneInfo("Europe/Prague")
    except ZoneInfoNotFoundError:  # tzdata absent — fixed CEST fallback
        return timezone(timedelta(hours=2), name="CEST")


def _prague_today() -> date:
    return datetime.now(_prague_tz()).date()


def _day_offset(wanted: date, prague_today: date) -> int:
    """Day-feed offset for ``wanted`` vs the source's Prague date."""
    return (wanted - prague_today).days


def _split_fields(chunk: str) -> dict[str, str]:
    """Pipe-format field splitter: ``KEY÷value¬KEY÷value...`` (see SOURCES.md).

    Runs on one ``~``-separated chunk, so neither ``¬`` nor ``~`` can appear
    inside values by construction; keys are 1-4 uppercase letters.
    """
    out: dict[str, str] = {}
    pos = 0
    length = len(chunk)
    while pos < length:
        sep = chunk.find("÷", pos)
        if sep < 0:
            break
        key = chunk[pos:sep]
        if not (1 <= len(key) <= 4 and key.isupper()):
            break  # not a field boundary — stop scanning this chunk
        end = chunk.find("¬", sep + 1)
        if end < 0:
            end = length
        out[key] = chunk[sep + 1 : end]
        pos = end + 1
    return out


def _as_int(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


def _start_time_utc(timestamp: str | None) -> str | None:
    value = _as_int(timestamp)
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


class FlashscoreAdapter(SourceAdapter):
    """Flashscore adapter: httpx + the verified ``x-fsign`` day-feed path."""

    source: ClassVar[str] = "flashscore"

    # -- contract -----------------------------------------------------------

    def fetch(self, sport: str, category: str, params: Any) -> SourceResponse:
        sport, category = str(sport), str(category)
        if sport not in _SPORTS:
            raise BadRequest(f"unknown sport {sport!r}", source=self.source)
        if category not in _CATEGORIES:
            raise BadRequest(f"unknown category {category!r}", source=self.source)
        if category == "h2h":
            raise self._h2h_unavailable()
        if category == "live":
            offset = 0  # live is "right now" -> the current Prague day doc
        else:
            offset = self._offset_for(category, params)
        url = f"{FEED_BASE}/f_{_SPORT_IDS[sport]}_{offset}_{_FEED_TAIL}"
        return self._request(url)

    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        text = self._decode_payload(resp)
        parts = text.split("~")
        if not parts or not parts[0].startswith("SA÷"):
            raise NoData(
                "payload is not a flashscore day-feed response (feed header "
                f"'SA÷…' missing; got {text[:60]!r})",
                source=resp.source,
            )
        header = _split_fields(parts[0])
        sport = _SPORT_BY_ID.get(header.get("SA", ""))
        if sport is None:
            raise NoData(
                f"unrecognized feed sport id SA={header.get('SA')!r} in the "
                "feed header (no rows parsed — never guess a sport)",
                source=resp.source,
            )
        warnings: list[str] = [
            parse_warning(
                "day-table feed returns the full source day with truthful "
                "statuses and carries no live clock/period detail (the AC "
                "field is not the live minute — disproven on probe) — "
                "fixtures/live/results filtering and live_detail are "
                "caller-side",
                resp,
            )
        ]
        events: list[Event] = []
        skipped: dict[str, int] = {}
        lines_dropped = 0
        league: dict[str, str] = {}
        for chunk in parts[1:]:
            if chunk.startswith("ZA÷"):
                league = _split_fields(chunk)
                continue
            if not chunk.startswith("AA÷"):
                continue
            raw = _split_fields(chunk)
            try:
                event = self._normalize(raw, sport, league)
            except _SkipEvent as exc:
                reason = str(exc)
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            lines, dropped = self._score_lines(sport, event.status, raw)
            if dropped:
                lines_dropped += 1
            if lines:
                event = event.model_copy(update={"score_lines": lines})
            events.append(event)
        for reason, count in sorted(skipped.items()):
            warnings.append(parse_warning(f"skipped {count} event(s): {reason}", resp))
        if lines_dropped:
            warnings.append(
                parse_warning(
                    f"{lines_dropped} finished basketball event(s) shipped "
                    "without quarter lines (period pairs missing or not "
                    "summing to the final score — overtime/unverified shape)",
                    resp,
                )
            )
        return ParseOutcome(events=stamp_provenance(resp, events), warnings=warnings)

    # -- fetch internals ----------------------------------------------------

    def _offset_for(self, category: str, params: Any) -> int:
        """Day offset for fixtures/results, from the date param or a default.

        Defaults: fixtures -> Prague today (0), results -> Prague yesterday
        (-1, where the day's matches are all finished). Only offsets in
        VERIFIED_DAY_OFFSETS (-1/0/+1, probed live 2026-09-02) may be
        requested; anything else is an honest ``NotFound`` with evidence —
        never an unverified feed guess.
        """
        raw_date = str(params.get("date") or "").strip() if hasattr(params, "get") else ""
        if raw_date:
            try:
                wanted = date.fromisoformat(raw_date)
            except ValueError:
                raise BadRequest(
                    f"date must be YYYY-MM-DD, got {raw_date!r}", source=self.source
                ) from None
        else:
            today = _prague_today()
            wanted = today if category == "fixtures" else today - timedelta(days=1)
        offset = _day_offset(wanted, _prague_today())
        if offset not in VERIFIED_DAY_OFFSETS:
            raise NotFound(
                f"day offset {offset} (date {wanted.isoformat()}) has no "
                "verified flashscore day-feed: only offsets "
                f"{list(VERIFIED_DAY_OFFSETS)} were live-probed 2026-09-02 "
                "(f_<sport>_<offset>_3_en_2 -> HTTP 200) — re-probe before "
                "shipping this range",
                source=self.source,
            )
        return offset

    def _request(self, url: str) -> SourceResponse:
        """GET ``url`` with retry discipline; returns or raises a typed error."""
        attempt = 0
        while True:
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(_CONNECT_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
                    headers=_HEADERS,
                    follow_redirects=False,
                ) as client:
                    resp = client.get(url)
            except httpx.TransportError as exc:
                if attempt < _MAX_RETRIES:
                    _sleep_backoff(attempt)
                    attempt += 1
                    continue
                raise SourceUnavailable(
                    f"request failed after {attempt + 1} attempt(s): {exc} — {url}",
                    source=self.source,
                ) from exc
            if resp.status_code >= 500 and attempt < _MAX_RETRIES:
                _sleep_backoff(attempt)
                attempt += 1
                continue
            return _map_http_outcome(url, resp.status_code, resp.content, _utc_now_iso(), self.source)

    # -- typed-error cells (evidence in detail; no fabricated data) ----------

    def _h2h_unavailable(self) -> PrimeSportDataError:
        return NotFound(
            "flashscore has no verified path from team names to head-to-head "
            "meetings: the per-event feed df_hh_<sport>_<eventId> was verified "
            "live (2026-09-02: df_hh_1_KxrZoe94 -> HTTP 200, recorded under "
            "tests/fixtures/flashscore/h2h/) but it requires a flashscore "
            "event id, and no team-name -> event resolution chain (search/"
            "team pages) was probed this session; source ids are not shared "
            "across sources, so the SPEC h2h envelope cannot be built "
            "honestly. Evidence: docs/source-health.md.",
            source=self.source,
        )

    def _escalate_to_scrapling(self, url: str) -> str:
        """Scrapling fallback hook — inactive this build by design.

        The plain-httpx ``x-fsign`` feed path was verified live on 2026-09-02
        and is the shipped mechanism, so nothing in ``fetch()`` calls this
        hook today. It exists so a later session whose plain fetch starts
        failing has a defined, tested escalation point; it is NOT a
        block/CAPTCHA bypass (SPEC: none, ever). The ``research`` extra
        (scrapling) is imported lazily here only — module import never
        requires it, and its absence raises ``DependencyMissing``.
        """
        try:
            import scrapling  # type: ignore[import-not-found]  # noqa: F401  # optional extra, lazy on purpose
        except ImportError as exc:
            raise DependencyMissing(
                f"scrapling (prime-sportdata[research]) is not installed — "
                f"cannot run a JS-rendered fallback fetch of {url}",
                source=self.source,
            ) from exc
        raise SourceUnavailable(
            f"scrapling fallback for {url} is not wired into fetch() this "
            "build: the plain-httpx feed path verified 2026-09-02 remains the "
            "shipped mechanism; enable only after a probe evidences the "
            "fallback shape (docs/source-health.md)",
            source=self.source,
        )

    # -- normalization ------------------------------------------------------

    @staticmethod
    def _decode_payload(resp: SourceResponse) -> str:
        if isinstance(resp.payload, str):
            return resp.payload
        if isinstance(resp.payload, bytes):
            return resp.payload.decode("utf-8", errors="replace")
        raise NoData(
            f"payload is not flashscore pipe text (got {type(resp.payload).__name__})",
            source=resp.source,
        )

    def _normalize(self, raw: dict[str, str], sport: Sport, league: dict[str, str]) -> Event:
        event_id = (raw.get("AA") or "").strip()
        home_name = (raw.get("AE") or "").strip()
        away_name = (raw.get("AF") or "").strip()
        if not event_id:
            raise _SkipEvent("event missing AA id")
        if not home_name or not away_name:
            raise _SkipEvent("event missing a competitor name")
        ab = raw.get("AB")
        ag, ah = _as_int(raw.get("AG")), _as_int(raw.get("AH"))
        status: EventStatus
        if ab == "1":
            status = "scheduled"
        elif ab == "2":
            status = "live"
        elif ab == "3":
            if ag is not None and ah is not None:
                status = "finished"
            elif raw.get("AC") == "4":
                status = "postponed"
            else:
                raise _SkipEvent(
                    f"AB=3 without a score and no AC=4 marker (unclassified "
                    f"end state, AC={raw.get('AC')!r})"
                )
        else:
            raise _SkipEvent(f"unhandled status code AB={ab!r}")
        if status == "scheduled":
            score_home = score_away = None
            current = False
        else:
            score_home, score_away = ag, ah
            current = status == "live"
        return Event(
            sport=sport,
            external=ExternalRef(source=self.source, source_event_id=event_id),
            start_time_utc=_start_time_utc(raw.get("AD")),
            status=status,
            home=Team(name=home_name, score=score_home, current_score=current),
            away=Team(name=away_name, score=score_away, current_score=current),
            score_lines=[],
            competition=self._competition(league),
            live_detail=None,
            round_label=None,
        )

    @staticmethod
    def _competition(league: dict[str, str]) -> Competition | None:
        """Competition from a league block (ZA/ZY/ZEE/ZL — see SOURCES.md).

        ``ZA`` is "<country-or-tour>: <name>" (e.g. "ENGLAND: EFL Trophy",
        "ATP - SINGLES: US Open (USA), hard") — the name is the part after
        the first ": "; ``ZY`` supplies the country when present and not a
        non-country sentinel ("World"/"Europe"/"International" -> None, same
        as the sofascore adapter treats UEFA-style competitions).
        """
        za = (league.get("ZA") or "").strip()
        if not za:
            return None
        _, sep, rest = za.partition(": ")
        name = (rest if sep else za).strip()
        if not name:
            return None
        zy = (league.get("ZY") or "").strip() or None
        if zy is not None and zy.casefold() in _NON_COUNTRY:
            zy = None
        return Competition(name=name, country=zy)

    @staticmethod
    def _score_lines(
        sport: Sport, status: EventStatus, raw: dict[str, str]
    ) -> tuple[list[ScoreLine], bool]:
        """Period/set lines per sport; (lines, needs_warning).

        Football: none — the BC/BD pair is not the half-time score
        (disproven 2026-09-02 vs cross-source kickoff/HT evidence, module
        docstring), and no FT/ET split exists in the day feed.
        Basketball (finished): Q1..Q4 from BA:BB..BG:BH, only when all four
        pairs are integers AND sum to the final score AG/AH (verified on all
        7 recorded finished rows; an overtime game would break the sum and
        correctly drops the lines with a warning instead of mislabeling).
        Tennis: S1.. from BA:BB..BI:BJ (games per set; AG/AH = sets won).
        Live rows carry the in-progress set's pair, shipped as its S-line
        exactly like the sofascore adapter ships in-play sets.
        """
        lines: list[ScoreLine] = []
        if sport == "football":
            return lines, False
        if sport == "basketball":
            if status != "finished":
                return lines, False
            pairs = ["BA", "BB", "BC", "BD", "BE", "BF", "BG", "BH"]
            quarter = [_as_int(raw.get(k)) for k in pairs]
            ag, ah = _as_int(raw.get("AG")), _as_int(raw.get("AH"))
            if None in quarter or ag is None or ah is None:
                return lines, False
            assert all(q is not None for q in quarter)  # narrow for mypy
            home_q = [quarter[i] for i in (0, 2, 4, 6)]
            away_q = [quarter[i] for i in (1, 3, 5, 7)]
            if sum(home_q) != ag or sum(away_q) != ah:  # type: ignore[arg-type]
                return lines, True
            for n in range(4):
                lines.append(
                    ScoreLine(
                        period_label=f"Q{n + 1}",
                        home=home_q[n],
                        away=away_q[n],
                    )
                )
            return lines, False
        # tennis
        set_keys = ["BA", "BB", "BC", "BD", "BE", "BF", "BG", "BH", "BI", "BJ"]
        n = 1
        for i in range(0, len(set_keys), 2):
            home = _as_int(raw.get(set_keys[i]))
            away = _as_int(raw.get(set_keys[i + 1]))
            if home is None and away is None:
                break
            if home is None or away is None:
                break  # partial pair — stop; never guess a set label
            lines.append(ScoreLine(period_label=f"S{n}", home=home, away=away))
            n += 1
        return lines, False


class _SkipEvent(Exception):
    """Internal: this raw event cannot be normalized honestly; skip + warn."""


__all__ = [
    "FEED_BASE",
    "USER_AGENT",
    "VERIFIED_DAY_OFFSETS",
    "FlashscoreAdapter",
    "_day_offset",
    "_map_http_outcome",
    "_prague_today",
    "_split_fields",
]
