"""Typed error tests: every error carries source/detail; block vs rate distinct."""

import pytest

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

ALL_ERRORS = [
    SourceUnavailable,
    SourceBlocked,
    RateLimited,
    NotFound,
    NoData,
    BadRequest,
    DependencyMissing,
]


def test_every_error_carries_source_and_detail():
    for cls in ALL_ERRORS:
        err = cls("boom", source="flashscore")
        assert err.detail == "boom"
        assert err.source == "flashscore"
        assert isinstance(err, PrimeSportDataError)
        no_source = cls("boom")
        assert no_source.source is None


def test_block_and_rate_limited_are_distinctly_catchable():
    blocked = SourceBlocked("403", source="flashscore")
    rate = RateLimited("429", source="sofascore")
    assert not isinstance(blocked, RateLimited)
    assert not isinstance(rate, SourceBlocked)
    with pytest.raises(SourceBlocked):
        raise blocked
    with pytest.raises(RateLimited):
        raise rate
    # Both are caught by the shared base without string matching on messages.
    with pytest.raises(PrimeSportDataError):
        raise blocked


def test_codes_match_error_body_conventions():
    expected = {
        SourceUnavailable: "source_unavailable",
        SourceBlocked: "source_blocked",
        RateLimited: "rate_limited",
        NotFound: "not_found",
        NoData: "no_data",
        BadRequest: "bad_request",
        DependencyMissing: "dependency_missing",
    }
    for cls, code in expected.items():
        assert cls.code == code
        assert cls("x").code == code


def test_http_status_hints_are_upstream_only():
    assert SourceBlocked.http_status == 403
    assert RateLimited.http_status == 429
    assert NotFound.http_status == 404
    assert BadRequest.http_status == 400
    assert SourceUnavailable.http_status is None
    assert NoData.http_status is None
    assert DependencyMissing.http_status is None
