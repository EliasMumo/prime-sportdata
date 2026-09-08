"""Typed exceptions for the fetch stack.

Conventions (SPEC "Errors" / "Ban-risk policy"):

* Every adapter failure crosses the adapter boundary as exactly one of these
  classes - never a bare exception.
* ``SourceBlocked`` and ``RateLimited`` are *distinct* types so the
  engine-level circuit breaker (SPEC Ban-risk policy §2) can trip on them
  without string matching on messages or codes.
* ``http_status`` is only an upstream hint (the status observed from, or
  expected of, the source). Terminal HTTP mapping belongs to the engine -
  nothing here decides it.
"""

from typing import ClassVar


class PrimeSportDataError(Exception):
    """Base class for all typed errors.

    Every subclass carries ``code`` (mirrors the ErrorBody ``code`` the engine
    will emit), ``detail`` and the ``source`` host that raised, if any.
    """

    code: ClassVar[str] = "error"
    http_status: ClassVar[int | None] = None

    def __init__(self, detail: str, *, source: str | None = None) -> None:
        super().__init__(detail)
        self.detail: str = detail
        self.source: str | None = source


class SourceUnavailable(PrimeSportDataError):
    """Host unreachable: timeout, TCP/DNS failure, or 5xx storm.

    A stepping stone to terminal 502; not itself terminal (SPEC "Errors").
    """

    code = "source_unavailable"


class SourceBlocked(PrimeSportDataError):
    """403 / challenge page / bot-block signature. Trips the breaker (§2)."""

    code = "source_blocked"
    http_status = 403


class RateLimited(PrimeSportDataError):
    """HTTP 429 or an equivalent anti-scrape throttle. Trips the breaker (§2)."""

    code = "rate_limited"
    http_status = 429


class NotFound(PrimeSportDataError):
    """The requested path does not exist on this source (e.g. 404 wrong path).

    Evidence the endpoint shape needs re-probing, not a data answer.
    """

    code = "not_found"
    http_status = 404


class NoData(PrimeSportDataError):
    """Source answered but the content yielded no usable rows.

    Honest empty-or-error: never fabricate rows to make a response "work".
    """

    code = "no_data"


class BadRequest(PrimeSportDataError):
    """The query was rejected by the source (e.g. HTTP 400, bad params)."""

    code = "bad_request"
    http_status = 400


class DependencyMissing(PrimeSportDataError):
    """An optional dependency (e.g. the ``research``/scrapling extra) is absent."""

    code = "dependency_missing"


__all__ = [
    "BadRequest",
    "DependencyMissing",
    "NoData",
    "NotFound",
    "PrimeSportDataError",
    "RateLimited",
    "SourceBlocked",
    "SourceUnavailable",
]
