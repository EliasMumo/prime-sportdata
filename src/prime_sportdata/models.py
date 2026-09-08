"""Normalized pydantic v2 models (SPEC "Normalized event shape" / "Envelope").

Everything the engine emits or the API returns is typed here. The types are
strict so later stages (engine failover, circuit breaker, FastAPI server) can
rely on them: unknown sports/categories/statuses must fail validation loudly
rather than flow through silently.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Sport = Literal["football", "basketball", "tennis"]
Category = Literal["fixtures", "live", "results", "h2h", "odds", "odds_detailed"]
EventStatus = Literal["scheduled", "live", "finished", "postponed", "cancelled", "interrupted"]


class Team(BaseModel):
    """A competitor (football/basketball team or tennis player).

    ``score`` is this competitor's score in the match; ``current_score`` marks
    the in-play period where derivable (only meaningful while ``status ==
    "live"``; defaults to False for fixtures/results).
    """

    name: str
    source_id: str | int | None = None
    score: int | None = None
    current_score: bool = False


class ScoreLine(BaseModel):
    """One period/set result. ``period_label`` follows the SPEC conventions
    ("H1"|"H2"|"FT"|"Q1".."Q4"|"OT"|"S1"..) but stays a free string so odd
    source labels (e.g. "HT", "2nd set") can be carried honestly."""

    period_label: str
    home: int | None = None
    away: int | None = None


class Competition(BaseModel):
    """Tournament context. ``country`` is omitted (None) when not derivable."""

    name: str
    country: str | None = None


class ExternalRef(BaseModel):
    """Provenance of the event inside the answering source (SPEC: ids returned
    are the answering source's ids; no cross-source unification)."""

    source: str
    source_event_id: str
    source_url: str | None = None


class Event(BaseModel):
    """One normalized match (SPEC "Normalized event shape", v1)."""

    sport: Sport
    external: ExternalRef
    start_time_utc: str | None = None  # ISO-8601 | null (raw, adapter-normalized)
    status: EventStatus
    home: Team
    away: Team
    score_lines: list[ScoreLine] = []
    competition: Competition | None = None
    live_detail: str | None = None  # e.g. "73'", "Q3 04:12", "2nd set"
    round_label: str | None = None  # tennis stage / football round if cheap


class EventsPayload(BaseModel):
    """Envelope data for fixtures / live / results: a plain event list.

    ``extra="forbid"`` keeps the data-union classification unambiguous: an h2h
    payload can never be misread as a plain events list (and vice versa).
    """

    model_config = ConfigDict(extra="forbid")

    events: list[Event] = []


class H2HEntity(BaseModel):
    """One side of an h2h comparison. ``summary`` is a plain-language line
    derivable from the source (e.g. "W3 D1 L2 in last 6 vs {opponent}") or
    omitted (None) when not derivable (SPEC)."""

    name: str
    summary: str | None = None


class H2HPayload(BaseModel):
    """Envelope data for h2h queries (SPEC: events + both entities)."""

    model_config = ConfigDict(extra="forbid")

    events: list[Event] = []
    home: H2HEntity
    away: H2HEntity


class OddsQuote(BaseModel):
    """One normalized betting quote (odds upgrade, 2026-09-08).

    A quote is a source-anchored set of decimal prices for one fixture.  The
    ``bookmaker`` is the display label of the odds column actually parsed;
    scraped aggregator columns are recorded as such — never renamed into a
    named execution bookmaker.
    """

    sport: Sport
    external: ExternalRef
    start_time_utc: str | None = None  # ISO-8601 | null (adapter-normalized)
    home: str
    away: str
    competition: Competition | None = None
    market: str  # e.g. "1x2" (football) or "home_away" (2-way sports)
    bookmaker: str | None = None  # display column label (e.g. "betexplorer-default")
    prices: dict[str, float]  # selection key ("1"/"X"/"2" or "home"/"away") -> decimal > 1


class OddsPayload(BaseModel):
    """Envelope data for odds queries."""

    model_config = ConfigDict(extra="forbid")

    quotes: list[OddsQuote] = []


EnvelopeData = EventsPayload | H2HPayload | OddsPayload


class RequestParams(BaseModel):
    """Echo of the caller's query (SPEC envelope ``meta.request``)."""

    date: str | None = None  # YYYY-MM-DD
    league: str | None = None
    limit: int | None = None


class Meta(BaseModel):
    """Envelope provenance + honesty notes."""

    sport: Sport
    category: Category
    source: str
    cached: bool
    fetched_at_utc: str  # ISO-8601 UTC
    latency_ms: int
    request: RequestParams
    warnings: list[str] = []  # SPEC: honesty notes are surfaced here, never silent


class Envelope(BaseModel):
    """Every successful (200) response body (SPEC "Envelope")."""

    data: EnvelopeData
    meta: Meta


class ErrorDetail(BaseModel):
    """Inner error object (SPEC "Errors" JSON body)."""

    code: str
    detail: str
    source: str | None = None
    tried_sources: list[str] = []


class ErrorBody(BaseModel):
    """Every non-200 response body (SPEC)."""

    error: ErrorDetail
