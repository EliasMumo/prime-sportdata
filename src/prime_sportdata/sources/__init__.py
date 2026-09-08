"""Concrete source adapters (flashscore / sofascore / livescore, Stages 3-4).

The adapter contract shared by all of them lives in :mod:`prime_sportdata.sources.base`.
"""

from prime_sportdata.sources.base import ParseOutcome, SourceAdapter, SourceResponse

__all__ = ["ParseOutcome", "SourceAdapter", "SourceResponse"]
