"""Cross-source football-results merge: half-time enrichment + union backup.

The flashscore day table is primary for football results but ships final
scores only.  The livescore date API carries half-time scores and postponed
statuses for the same day.  This module merges the two:

* a livescore event matching a flashscore event (normalized names +
  kickoff window) adds its H1/FT score lines when the primary lacks them;
* a livescore event with no flashscore counterpart is appended (union), so
  fixtures the primary never carried (moved day, postponed, dropped) still
  reach callers instead of vanishing from the results feed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime

from prime_sportdata.models import Event

# Source spelling variants seen in the wild -> canonical (livescore-style)
# long names. Kept deliberately small; containment below covers the rest.
_CROSS_SOURCE_ALIASES: dict[str, str] = {
    "atl madrid": "atletico madrid",
    "ath madrid": "atletico madrid",
    "dep a coruna": "deportivo la coruna",
    "dep coruna": "deportivo la coruna",
    "deportivo coruna": "deportivo la coruna",
    "r santander": "racing santander",
    "racing de santander": "racing santander",
    "ath bilbao": "athletic club",
    "ath club": "athletic club",
}

_KICKOFF_WINDOW_SECONDS = 3 * 3600  # ±3 h absorbs source timezone quirks


def normalize_team_name(value: object) -> str:
    """Lowercase, ascii-fold, strip punctuation; drop single letters."""
    folded = unicodedata.normalize("NFKD", str(value or "").casefold())
    ascii_name = folded.encode("ascii", "ignore").decode()
    tokens = [token for token in re.split(r"[^a-z0-9]+", ascii_name) if len(token) > 1]
    return " ".join(tokens)


def canonical_team_name(value: object) -> str:
    norm = normalize_team_name(value)
    return _CROSS_SOURCE_ALIASES.get(norm, norm)


def _kickoff_utc(event: Event) -> datetime | None:
    raw = event.start_time_utc
    if not raw:
        return None
    try:
        kickoff = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if kickoff.tzinfo is None:
        # Naive source timestamps (livescore Esd) are treated as UTC.
        kickoff = kickoff.replace(tzinfo=UTC)
    return kickoff


def _token_contained(left: str, right: str) -> bool:
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if not left_tokens or not right_tokens:
        return False
    return left_tokens <= right_tokens or right_tokens <= left_tokens


def _names_match(primary: Event, secondary: Event) -> bool:
    p_home, p_away = canonical_team_name(primary.home.name), canonical_team_name(primary.away.name)
    s_home, s_away = canonical_team_name(secondary.home.name), canonical_team_name(secondary.away.name)
    if (p_home == s_home and p_away == s_away) or (p_home == s_away and p_away == s_home):
        return True
    return (_token_contained(p_home, s_home) and _token_contained(p_away, s_away)) or (
        _token_contained(p_home, s_away) and _token_contained(p_away, s_home)
    )


def merge_football_results(
    primary: list[Event],
    secondary: Iterable[Event],
) -> tuple[list[Event], list[str]]:
    """Merge secondary (livescore) events into the primary (flashscore) list.

    Returns the merged list and human-readable warnings.  Matching requires
    normalized-name agreement plus, when both kickoffs exist, agreement
    within ±3 h — a live score line is never attached across different
    matches.
    """
    warnings: list[str] = []
    merged: list[Event] = list(primary)
    enriched = 0
    appended = 0
    for secondary_event in secondary:
        match: Event | None = None
        for primary_event in merged:
            if not _names_match(primary_event, secondary_event):
                continue
            primary_kickoff = _kickoff_utc(primary_event)
            secondary_kickoff = _kickoff_utc(secondary_event)
            if (
                primary_kickoff is not None
                and secondary_kickoff is not None
                and abs((primary_kickoff - secondary_kickoff).total_seconds())
                > _KICKOFF_WINDOW_SECONDS
            ):
                continue
            match = primary_event
            break
        if match is not None:
            existing_labels = {line.period_label for line in match.score_lines}
            added = [
                line
                for line in secondary_event.score_lines
                if line.period_label not in existing_labels
            ]
            if added:
                match.score_lines.extend(added)
                enriched += 1
            continue
        merged.append(secondary_event)
        appended += 1
    if enriched:
        warnings.append(
            f"football results: half-time/period lines enriched from livescore "
            f"for {enriched} event(s)"
        )
    if appended:
        warnings.append(
            f"football results: appended {appended} livescore-only event(s) "
            "(flashscore-miss backup)"
        )
    return merged, warnings


__all__ = [
    "canonical_team_name",
    "merge_football_results",
    "normalize_team_name",
]
