"""Source adapter contract: ABC + ``SourceResponse`` + shared parse helpers.

Contract (SPEC: honest data only — never fabricate rows):

* ``SourceAdapter.fetch`` returns a real ``SourceResponse`` (something that
  was actually requested over HTTP) and raises only the typed errors from
  :mod:`prime_sportdata.errors` — never bare exceptions across the boundary.
* Parsing happens against a real ``SourceResponse``: ``parse_json_response``
  decodes its payload, and ``stamp_provenance`` anchors every parsed event to
  the response it came from (source name + real requested URL). Nothing in
  core can claim an event that was parsed "from nowhere".

Concrete adapters are Stage 3/4 work; only the shared contract lives here.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from prime_sportdata.errors import NoData
from prime_sportdata.models import Event, ExternalRef, OddsQuote


@dataclass(frozen=True)
class SourceResponse:
    """What a fetch actually produced, with full provenance.

    ``fetched_at`` is ISO-8601 UTC as recorded by the adapter when the
    response arrived. ``payload`` is the raw body (bytes/str) or an
    already-decoded JSON dict.
    """

    source: str
    payload: bytes | str | dict[str, Any]
    url: str
    status: int | None
    fetched_at: str


@dataclass(frozen=True)
class ParseOutcome:
    """Result of parsing one SourceResponse into normalized events/odds."""

    events: list[Event] = field(default_factory=list)
    quotes: list[OddsQuote] = field(default_factory=list)
    lineups: dict[str, Any] | None = None  # set only by parse_lineups
    warnings: list[str] = field(default_factory=list)


class SourceAdapter(ABC):
    """Interface every concrete adapter (flashscore/sofascore/livescore) implements.

    Subclasses MUST raise only ``PrimeSportDataError`` subtypes out of
    ``fetch``/``parse_events`` (typed errors; SPEC honesty rules) and must set
    ``source`` to their own name (e.g. ``"flashscore"``).
    """

    source: ClassVar[str] = ""

    @abstractmethod
    def fetch(self, sport: str, category: str, params: Mapping[str, str]) -> SourceResponse:
        """Perform the real HTTP request for ``sport/category`` with the given
        (already normalized, string-valued) query params.

        Returns the raw response; never returns data that was not actually
        fetched, never invents rows when the source is down or empty.
        """

    @abstractmethod
    def parse_events(self, resp: SourceResponse) -> ParseOutcome:
        """Turn one fetched ``SourceResponse`` into normalized events + honesty
        warnings. Raises typed errors (e.g. ``NoData``) when the content is
        unusable instead of returning fabricated rows."""

    def parse_odds(self, resp: SourceResponse) -> ParseOutcome:
        """Turn one fetched ``SourceResponse`` into normalized betting quotes.

        The default raises ``NoData``: an adapter only supports this method
        when it ships a live-verified odds data path (SPEC: no invented
        rows, no parsers against unverified shapes).
        """
        raise NoData(
            f"source {self.source} has no verified odds data path",
            source=resp.source,
        )

    def parse_lineups(self, resp: SourceResponse) -> ParseOutcome:
        """Turn one fetched ``SourceResponse`` into a normalized lineups
        payload (``outcome.lineups``) plus the matched fixture event.

        The default raises ``NoData``: an adapter only supports this method
        when it ships a live-verified lineups data path.
        """
        raise NoData(
            f"source {self.source} has no verified lineups data path",
            source=resp.source,
        )


def parse_json_response(resp: SourceResponse) -> Any:
    """Decode ``resp.payload`` (bytes/str/dict) into JSON.

    A healthy source whose content cannot be decoded raises ``NoData``
    (SPEC: source answered but content unusable) — never a bare exception.
    """
    if isinstance(resp.payload, dict):
        return resp.payload
    try:
        return json.loads(resp.payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NoData(f"payload is not valid JSON: {exc}", source=resp.source) from exc


def parse_warning(note: str, resp: SourceResponse) -> str:
    """Format a warnings[] entry per the SPEC honesty convention, e.g.
    ``parse_warning("endpoint shape unverified — parses may break", resp)``.

    Adapters add such lines whenever they ship code against imperfectly
    verified endpoint shapes — flagged, never silent.
    """
    return f"{note} (source={resp.source}, url={resp.url})"


def stamp_provenance(resp: SourceResponse, events: Iterable[Event]) -> list[Event]:
    """Anchor parsed events to the real response they came from.

    Returns copies of ``events`` whose ``external.source`` matches the adapter
    that actually fetched ``resp`` and whose ``source_url`` falls back to the
    URL that was really requested. This makes "parsed from a real
    SourceResponse" structural: an event claiming a different source than the
    one that answered is corrected, never silently trusted.
    """
    anchored: list[Event] = []
    for event in events:
        external = event.external
        if external.source == resp.source and external.source_url is not None:
            anchored.append(event)
            continue
        anchored.append(
            event.model_copy(
                update={
                    "external": ExternalRef(
                        source=resp.source,
                        source_event_id=external.source_event_id,
                        source_url=external.source_url or resp.url,
                    )
                }
            )
        )
    return anchored
