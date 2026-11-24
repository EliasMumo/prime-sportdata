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
  live (``df_hh_1_KxrZoe94`` -> 200, recorded under
  ``tests/fixtures/flashscore/h2h/``; the full 2026-11-24 capture carries the
  target row plus 14 real prior meetings, all predating the kickoff).  There is
  still no team-name -> event resolution chain (no search/team path was
  probed), so h2h only runs when
  the caller supplies a *verified* flashscore event id plus the scheduled
  kickoff and both entity names.  The recorded feed carries the target event
  row itself (``KP`` = event id, ``KC`` = kickoff, ``KJ``/``KK`` = home/away,
  ``KL``/``KU``/``KT`` = final score), which corroborates the id: the adapter
  requires the row's ordered participants and its ``KC`` to match the call,
  then returns only completed meetings of the same pair strictly before that
  kickoff.  Name-only calls keep the pre-network ``NotFound`` (unchanged);
  an id without a kickoff is an honest empty h2h answer (``NoData``, no
  request sent) because the time filter would be unverified.  Only football
  is enabled — the ``df_hh_1_*`` shape is the recorded one; ids 2/3 were never
  probed.

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
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
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
    MatchSummaryPayload,
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
_CATEGORIES = ("fixtures", "live", "results", "h2h", "match_summary")
_SPORT_IDS: dict[str, int] = {"football": 1, "tennis": 2, "basketball": 3}
_SPORT_BY_ID: dict[str, Sport] = {"1": "football", "2": "tennis", "3": "basketball"}
# Day-feed shape suffix; only day offsets within VERIFIED_DAY_OFFSETS may be
# requested.  Offsets -1..+1 were probed live on 2026-09-02; offsets -7..-2
# were probed live on 2026-09-15 for football, tennis, and basketball
# (f_<s>_<offset>_3_en_2 -> HTTP 200 with data; offset -8 -> 401).
_FEED_TAIL = "3_en_2"
# h2h feeds verified from a real recorded body: df_hh_1_<eventId> (football).
# The tennis/basketball ids (2/3) follow the same resolver but were never
# probed, so those sports keep the existing typed-error cells.
_H2H_VERIFIED_SPORTS: frozenset[str] = frozenset({"football"})
_H2H_SCORE_RE = re.compile(r"^(\d+):(\d+)$")
VERIFIED_DAY_OFFSETS: tuple[int, ...] = tuple(range(-7, 2))
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


def _clean_team_name(value: str) -> str:
    """Display name with flashscore's leading winner marker (``*``) removed."""
    return value.strip().lstrip("*").strip()


def _team_key(value: str) -> str:
    """Comparison key for a team name (case/whitespace/marker insensitive)."""
    return " ".join(_clean_team_name(value).casefold().split())


def _kickoff_epoch(value: str) -> int | None:
    """Unix seconds for a caller-supplied ISO-8601 kickoff (None when absent).

    A naive timestamp is refused: the "strictly before kickoff" filter would
    otherwise depend on an unstated timezone.
    """
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise BadRequest(
            f"kickoff must be ISO-8601 (e.g. 2026-09-02T13:00:00Z), got {value!r}",
            source="flashscore",
        ) from None
    if parsed.tzinfo is None:
        raise BadRequest(
            f"kickoff must carry a UTC offset (got a naive timestamp {value!r})",
            source="flashscore",
        )
    return int(parsed.timestamp())


@dataclass(frozen=True)
class _H2HMeeting:
    """One row of the recorded ``df_hh_<sport>_<eventId>`` feed.

    Field mapping (recorded 2026-09-02): ``KP`` event id, ``KC`` kickoff unix
    seconds, ``KJ``/``KK`` home/away display names, ``KU``/``KT`` final score,
    ``KL`` "home:away" score text, ``KF`` competition, ``KH`` country, ``KS``
    venue of the anchored team, and ``KB`` opens a "Last matches: <team>"
    section whose team is the anchor for ``KS``.
    """

    row_index: int
    event_id: str
    kickoff: int | None
    home: str
    away: str
    home_key: str
    away_key: str
    home_score: int | None
    away_score: int | None
    score_text: str | None
    competition: str | None
    country: str | None
    anchor: str | None
    venue: str | None


class FlashscoreAdapter(SourceAdapter):
    """Flashscore adapter: httpx + verified day-feed and per-event h2h paths."""

    source: ClassVar[str] = "flashscore"

    # -- contract -----------------------------------------------------------

    def fetch(self, sport: str, category: str, params: Any) -> SourceResponse:
        sport, category = str(sport), str(category)
        if sport not in _SPORTS:
            raise BadRequest(f"unknown sport {sport!r}", source=self.source)
        if category not in _CATEGORIES:
            raise BadRequest(f"unknown category {category!r}", source=self.source)
        if category == "h2h":
            event_id, entity_a, entity_b, kickoff = self._h2h_params(params)
            if not event_id:
                # Name-only lookups keep the pre-network typed error: no
                # team-name -> event resolution chain was ever probed.
                raise self._h2h_unavailable()
            if not entity_a or not entity_b:
                raise BadRequest(
                    "h2h requires both entity_a and entity_b to corroborate "
                    "the verified event id",
                    source=self.source,
                )
            if sport not in _H2H_VERIFIED_SPORTS:
                raise NotFound(
                    "flashscore h2h is probe-verified only for football "
                    "(df_hh_1_<eventId>); no recorded response exists for "
                    f"sport id {_SPORT_IDS[sport]} — re-probe before shipping",
                    source=self.source,
                )
            if kickoff is None:
                # The id alone cannot bound the meeting list: the "strictly
                # before kickoff" filter would be a guess, so answer with an
                # honest empty h2h cell instead of fetching.
                raise NoData(
                    "h2h with a verified event id also needs the scheduled "
                    "kickoff (ISO-8601, e.g. 2026-09-02T13:00:00Z) to bound "
                    "the meeting list; no request sent",
                    source=self.source,
                )
            return self._request(f"{FEED_BASE}/df_hh_{_SPORT_IDS[sport]}_{event_id}")
        if category == "match_summary":
            event_id, _, _ = self._summary_params(params)
            return self._request(f"{FEED_BASE}/df_su_1_{event_id}")
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

    @staticmethod
    def _summary_params(params: Any) -> tuple[str, int, int]:
        """(event_id, home_score, away_score) validated from the request.

        The finals are a required cross-validation input: the summary feed
        carries per-half goal counts but no final score of its own, and the
        SPEC forbids shipping period lines that cannot be reproduced from
        the caller-verified final.
        """
        get = params.get if hasattr(params, "get") else None
        if get is None:
            raise BadRequest(
                "match_summary params must be a mapping", source="flashscore"
            )
        event_id = str(get("event") or "").strip()
        if not event_id:
            raise BadRequest(
                "match_summary requires the flashscore source event id",
                source="flashscore",
            )
        home = _as_int(str(get("home_score")))
        away = _as_int(str(get("away_score")))
        if home is None or away is None:
            raise BadRequest(
                "match_summary requires integer home_score and away_score to "
                "cross-validate the period sums",
                source="flashscore",
            )
        return event_id, home, away

    @staticmethod
    def _h2h_params(params: Any) -> tuple[str, str, str, int | None]:
        """(event_id, entity_a, entity_b, kickoff unix seconds) from the request.

        ``event`` absent is the legacy name-only call (no verified name -> id
        resolution exists, so ``fetch`` keeps raising ``NotFound``).  A
        malformed or timezone-naive ``kickoff`` is a caller error.
        """
        get = params.get if hasattr(params, "get") else None
        if get is None:
            raise BadRequest("h2h params must be a mapping", source="flashscore")
        event_id = str(get("event") or "").strip()
        entity_a = str(get("entity_a") or "").strip()
        entity_b = str(get("entity_b") or "").strip()
        kickoff = _kickoff_epoch(str(get("kickoff") or ""))
        return event_id, entity_a, entity_b, kickoff

    def parse_match_summary(
        self, resp: SourceResponse, *, home_score: int | None, away_score: int | None
    ) -> ParseOutcome:
        """Parse the per-event summary feed into validated H1/H2 lines.

        ``df_su_1_<eventId>`` (live-verified 2026-10-05: The Strongest vs
        Universitario de Vinto, O6KrS6uP) carries per-half goal counts
        (``AC`` section label + ``IG``/``IH`` integers).  Lines ship only
        when the half sums reproduce the caller-provided final score
        exactly — otherwise ``NoData`` with the evidence.  Never a guessed
        period line.
        """
        if home_score is None or away_score is None:
            raise NoData(
                "match_summary requires integer home_score/away_score to "
                "validate period sums",
                source=resp.source,
            )
        text = self._decode_payload(resp)
        halves: dict[str, tuple[int, int]] = {}
        for chunk in text.split("~"):
            fields = _split_fields(chunk)
            label = (fields.get("AC") or "").strip()
            if label not in ("1st Half", "2nd Half", "Extra Time", "Penalties"):
                continue
            ig, ih = _as_int(fields.get("IG")), _as_int(fields.get("IH"))
            if ig is None or ih is None:
                raise NoData(
                    f"summary section {label!r} lacks integer IG/IH goal counts",
                    source=resp.source,
                )
            halves[label] = (ig, ih)
        if "1st Half" not in halves:
            raise NoData(
                "summary feed has no 1st Half section (no verified half-time score)",
                source=resp.source,
            )
        first = halves["1st Half"]
        second = halves.get("2nd Half")
        home_sum = first[0] + (second[0] if second is not None else 0)
        away_sum = first[1] + (second[1] if second is not None else 0)
        if home_sum != home_score or away_sum != away_score:
            second_text = f", 2nd {second[0]}-{second[1]}" if second is not None else ""
            raise NoData(
                "summary period sums do not match the caller-provided finals: "
                f"1st {first[0]}-{first[1]}{second_text} sums to "
                f"{home_sum}-{away_sum}, finals {home_score}-{away_score}",
                source=resp.source,
            )
        lines = [ScoreLine(period_label="H1", home=first[0], away=first[1])]
        if second is not None:
            lines.append(ScoreLine(period_label="H2", home=second[0], away=second[1]))
        return ParseOutcome(
            summary=MatchSummaryPayload(score_lines=lines),
            warnings=[
                parse_warning(
                    "period lines parsed from the per-event summary feed "
                    "(df_su_1_<id>) and cross-validated against the "
                    "caller-provided final score",
                    resp,
                )
            ],
        )

    def parse_h2h(self, resp: SourceResponse, *, params: Mapping[str, str]) -> ParseOutcome:
        """Parse ``df_hh_<sportId>_<eventId>`` into corroborated pair meetings.

        Honesty rules (recorded feed shape, tests/fixtures/flashscore/h2h/):

        * The verified event id must appear in the feed; that row's ordered
          participants (``KJ``/``KK``) must equal ``entity_a``/``entity_b`` and
          its kickoff (``KC``) must equal the caller's scheduled kickoff.  A
          reversed or missing participants row, an ambiguous/duplicated target
          row or a kickoff mismatch raises ``NoData`` — never a guessed pair.
        * Only completed meetings of the same pair (either home/away order,
          scored by ``KU``/``KT``/``KL``) strictly before the kickoff are
          returned.  Rows at/after the kickoff are dropped (the target event
          itself is one of them).
        * Missing identity (``KP``), unusable timestamps and conflicting
          duplicate rows make the meeting list unavailable (``NoData``); the
          other sections of the feed (each team's own recent matches) are
          never shipped as h2h.
        """
        event_id, entity_a, entity_b, kickoff_epoch = self._h2h_params(params)
        if not event_id:
            raise self._h2h_unavailable()
        if kickoff_epoch is None:
            raise NoData(
                "h2h parsing requires the verified scheduled kickoff "
                "(kickoff=<ISO-8601>) to bound the meeting list",
                source=resp.source,
            )
        home_key, away_key = _team_key(entity_a), _team_key(entity_b)
        if not home_key or not away_key or home_key == away_key:
            raise NoData(
                "h2h participants cannot be told apart "
                f"(entity_a={entity_a!r}, entity_b={entity_b!r})",
                source=resp.source,
            )

        text = self._decode_payload(resp)
        parts = text.split("~")
        if not parts or not parts[0].startswith("SA÷"):
            raise NoData(
                "payload is not a flashscore h2h feed (feed header 'SA÷…' "
                f"missing; got {text[:60]!r})",
                source=resp.source,
            )
        sport = _SPORT_BY_ID.get(_split_fields(parts[0]).get("SA", ""))
        if sport is None:
            raise NoData(
                "unrecognized feed sport id in the h2h header (no rows parsed "
                "— never guess a sport)",
                source=resp.source,
            )

        rows = self._h2h_rows(parts)
        target = self._h2h_target(rows, event_id, home_key, away_key, kickoff_epoch, resp)
        self._verify_orientation(target, resp)

        events: list[Event] = []
        seen: dict[str, tuple[int, str, str, int, int]] = {}
        for row in rows:
            # The target event itself is not a previous meeting.
            if row.event_id == event_id:
                continue
            if not self._is_pair(row, home_key, away_key):
                continue
            self._verify_orientation(row, resp)
            if row.kickoff is None or row.kickoff <= 0:
                raise NoData(
                    f"h2h meeting {row.event_id or f'row {row.row_index}'} has "
                    "no usable timestamp (KC missing, not an integer or not a "
                    "real unix time) — it cannot be placed before the kickoff",
                    source=resp.source,
                )
            if row.kickoff >= kickoff_epoch:
                continue
            if not row.event_id:
                raise NoData(
                    f"h2h meeting on row {row.row_index} has no source event id "
                    "(KP missing) — its identity is unverified",
                    source=resp.source,
                )
            home_score, away_score = self._h2h_scores(row, resp)
            signature = (row.kickoff, row.home_key, row.away_key, home_score, away_score)
            previous = seen.get(row.event_id)
            if previous is not None:
                if previous != signature:
                    raise NoData(
                        f"h2h feed repeats event {row.event_id} with conflicting "
                        "details — the meeting is ambiguous",
                        source=resp.source,
                    )
                continue
            seen[row.event_id] = signature
            competition = None
            if row.competition:
                country = row.country
                if country is not None and country.casefold() in _NON_COUNTRY:
                    country = None
                competition = Competition(name=row.competition, country=country)
            events.append(
                Event(
                    sport=sport,
                    external=ExternalRef(source=self.source, source_event_id=row.event_id),
                    start_time_utc=_start_time_utc(str(row.kickoff)),
                    status="finished",
                    home=Team(name=row.home, score=home_score),
                    away=Team(name=row.away, score=away_score),
                    competition=competition,
                )
            )

        warnings = [
            parse_warning(
                "h2h meetings are limited to the corroborated pair's completed "
                f"matches strictly before the verified kickoff (event {event_id})",
                resp,
            )
        ]
        if not events:
            warnings.append(
                parse_warning(
                    "h2h feed carried no completed meeting of this pair before "
                    "the kickoff (valid empty — the feed also lists each team's "
                    "own recent matches under separate sections)",
                    resp,
                )
            )
        return ParseOutcome(events=stamp_provenance(resp, events), warnings=warnings)

    @staticmethod
    def _h2h_rows(parts: list[str]) -> list[_H2HMeeting]:
        """Split the recorded feed into meeting rows, tracking section anchors."""
        rows: list[_H2HMeeting] = []
        anchor: str | None = None
        for index, chunk in enumerate(parts[1:], start=1):
            fields = _split_fields(chunk)
            if not fields:
                continue
            section = (fields.get("KB") or "").strip()
            if section:
                anchor = (
                    _team_key(section.split(":", 1)[1])
                    if section.casefold().startswith("last matches:")
                    else None
                )
            home = _clean_team_name(fields.get("KJ") or "")
            away = _clean_team_name(fields.get("KK") or "")
            if not home or not away:
                continue
            rows.append(
                _H2HMeeting(
                    row_index=index,
                    event_id=(fields.get("KP") or "").strip(),
                    kickoff=_as_int(fields.get("KC")),
                    home=home,
                    away=away,
                    home_key=_team_key(home),
                    away_key=_team_key(away),
                    home_score=_as_int(fields.get("KU")),
                    away_score=_as_int(fields.get("KT")),
                    score_text=(fields.get("KL") or "").strip() or None,
                    competition=(fields.get("KF") or "").strip() or None,
                    country=(fields.get("KH") or "").strip() or None,
                    anchor=anchor,
                    venue=(fields.get("KS") or "").strip() or None,
                )
            )
        return rows

    @staticmethod
    def _h2h_target(
        rows: list[_H2HMeeting],
        event_id: str,
        home_key: str,
        away_key: str,
        kickoff_epoch: int,
        resp: SourceResponse,
    ) -> _H2HMeeting:
        """Find and corroborate the feed row for the verified event id."""
        targets = [row for row in rows if row.event_id == event_id]
        if not targets:
            raise NoData(
                f"h2h feed does not contain the verified event {event_id}: the "
                "event id cannot be corroborated (participants/kickoff are "
                "unverified) — refusing to return another pair's meetings",
                source=resp.source,
            )
        signatures = {(row.kickoff, row.home_key, row.away_key) for row in targets}
        if len(signatures) > 1:
            raise NoData(
                f"h2h feed carries conflicting rows for event {event_id} — the "
                "event identity is ambiguous",
                source=resp.source,
            )
        target = targets[0]
        if target.home_key == away_key and target.away_key == home_key:
            raise NoData(
                f"h2h event {event_id} is recorded as {target.home} vs "
                f"{target.away}; the request passes them reversed as "
                "entity_a/entity_b, so the home/away orientation is ambiguous "
                "— pass the fixture's home team as entity_a",
                source=resp.source,
            )
        if target.home_key != home_key or target.away_key != away_key:
            raise NoData(
                f"h2h event {event_id} is recorded as {target.home} vs "
                f"{target.away}, which does not match the requested "
                "participants — refusing to mix pairs",
                source=resp.source,
            )
        if target.kickoff is None:
            raise NoData(
                f"h2h event {event_id} row has no usable kickoff (KC missing or "
                "not an integer) — the scheduled kickoff is unverified",
                source=resp.source,
            )
        if target.kickoff != kickoff_epoch:
            actual = _start_time_utc(str(target.kickoff))
            expected = datetime.fromtimestamp(kickoff_epoch, tz=UTC).isoformat()
            raise NoData(
                f"h2h event {event_id} kickoff {actual} does not match the "
                f"verified scheduled kickoff {expected} — the event id may be "
                "stale or belong to another fixture",
                source=resp.source,
            )
        return target

    @staticmethod
    def _is_pair(row: _H2HMeeting, home_key: str, away_key: str) -> bool:
        """True when the row is a meeting between exactly this pair."""
        return (row.home_key == home_key and row.away_key == away_key) or (
            row.home_key == away_key and row.away_key == home_key
        )

    @staticmethod
    def _verify_orientation(row: _H2HMeeting, resp: SourceResponse) -> None:
        """Cross-check ``KJ``/``KK`` against the section anchor and ``KS``.

        The recorded feed groups meetings under "Last matches: <team>" and
        stamps ``KS`` with that team's venue (home/away).  When both are
        present they must agree with the row's home/away columns, otherwise
        the row's orientation is uncertain and the cell is unavailable.
        """
        if row.anchor is None or row.venue is None:
            return
        venue = row.venue.casefold()
        if venue not in ("home", "away"):
            return
        if row.home_key == row.anchor:
            expected = "home"
        elif row.away_key == row.anchor:
            expected = "away"
        else:
            raise NoData(
                f"h2h row {row.row_index} lists {row.home} vs {row.away} but its "
                f"section anchors on {row.anchor!r} — the row identity is "
                "uncertain",
                source=resp.source,
            )
        if venue != expected:
            raise NoData(
                f"h2h row {row.row_index} ({row.home} vs {row.away}) reports "
                f"KS={row.venue!r} while the anchored team {row.anchor!r} is "
                f"{expected} — the orientation is ambiguous",
                source=resp.source,
            )

    @staticmethod
    def _h2h_scores(row: _H2HMeeting, resp: SourceResponse) -> tuple[int, int]:
        """Home/away final score aligned with ``KJ``/``KK``; NoData if unusable."""
        match = _H2H_SCORE_RE.match(row.score_text or "")
        text_scores = (int(match.group(1)), int(match.group(2))) if match else None
        if row.home_score is None or row.away_score is None:
            if text_scores is None:
                raise NoData(
                    f"h2h meeting {row.event_id} has no completed score "
                    "(KU/KT and KL are missing or unusable) — it is not a "
                    "completed match",
                    source=resp.source,
                )
            return text_scores
        if row.score_text is not None and text_scores is None:
            raise NoData(
                f"h2h meeting {row.event_id} has a malformed KL score "
                f"{row.score_text!r}",
                source=resp.source,
            )
        if text_scores is not None and text_scores != (row.home_score, row.away_score):
            raise NoData(
                f"h2h meeting {row.event_id} score fields disagree: "
                f"KL={row.score_text!r} vs KU/KT={row.home_score}:{row.away_score}",
                source=resp.source,
            )
        return row.home_score, row.away_score

    def _offset_for(self, category: str, params: Any) -> int:
        """Day offset for fixtures/results, from the date param or a default.

        Defaults: fixtures -> Prague today (0), results -> Prague yesterday
        (-1, where the day's matches are all finished). Only offsets in
        VERIFIED_DAY_OFFSETS (-7..+1, probed live 2026-09-02 and
        2026-09-15) may be requested; anything else is an honest
        ``NotFound`` with evidence — never an unverified feed guess.
        """
        raw_date = (
            params.date
            if hasattr(params, "date")
            else (params.get("date") if hasattr(params, "get") else None)
        )
        if raw_date:
            try:
                wanted = date.fromisoformat(str(raw_date).strip())
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
                f"{list(VERIFIED_DAY_OFFSETS)} were live-probed "
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
            "flashscore h2h needs a verified event id: the per-event feed "
            "df_hh_<sport>_<eventId> was verified live (2026-09-02: "
            "df_hh_1_KxrZoe94 -> HTTP 200, recorded under "
            "tests/fixtures/flashscore/h2h/) and this adapter ships it when "
            "the caller passes event=<flashscore id> and "
            "kickoff=<ISO-8601 start>; no team-name -> event resolution chain "
            "(search/team pages) was probed, and source ids are not shared "
            "across sources, so a name-only h2h call cannot be built "
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
