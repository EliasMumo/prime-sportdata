"""Livescore adapter offline tests (graceful-blocked; no network, no fixtures).

No livescore response was EVER recorded on this network — every 2026-09-02
probe (SPEC evidence row + this build's re-probe) ended in a TCP connect
timeout — so there are no recorded fixtures and no synthetic "success parse"
tests: fabricating a response shape would be an invented contract (SPEC).
These tests cover the typed-error surface: transport failure ->
SourceUnavailable, 403/challenge -> SourceBlocked, 429 -> RateLimited, 5xx ->
SourceUnavailable, parse_events -> NoData on any input, and base-contract
conformance. The httpx transport is monkeypatched; no test touches the
network.
"""

from typing import Any, Self

import httpx
import pytest

from prime_sportdata.errors import (
    BadRequest,
    NoData,
    RateLimited,
    SourceBlocked,
    SourceUnavailable,
)
from prime_sportdata.sources import livescore as ls
from prime_sportdata.sources.base import SourceAdapter, SourceResponse

FETCHED_AT = "2026-09-02T20:00:00Z"
URL = ls.BASE_URL


@pytest.fixture()
def adapter() -> ls.LivescoreAdapter:
    return ls.LivescoreAdapter()


# --- fake transport (installed per test; never touches the network) -----------


def _install_fake_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: BaseException | None = None,
    status: int = 200,
) -> None:
    """Replace httpx.Client with a stub: every GET raises ``error`` or answers
    ``status`` with an empty body. Also no-ops the retry backoff so the
    MAX_RETRIES loop runs without sleeping."""

    class FakeResponse:
        status_code: int = status
        content: bytes = b""

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> FakeResponse:
            if error is not None:
                raise error
            return FakeResponse()

    monkeypatch.setattr(ls.httpx, "Client", FakeClient)
    monkeypatch.setattr(ls, "_sleep_backoff", lambda _attempt: None)


# --- base-contract conformance -------------------------------------------------


def test_adapter_satisfies_base_contract(adapter: ls.LivescoreAdapter) -> None:
    assert isinstance(adapter, SourceAdapter)
    assert type(adapter).__abstractmethods__ == frozenset()  # ABC fully implemented
    assert adapter.source == "livescore"
    assert ls.LivescoreAdapter.source == "livescore"


# --- fetch: transport failure -> typed SourceUnavailable (observed reality) ---


def test_fetch_transport_connect_error_raises_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport(
        monkeypatch, error=httpx.ConnectTimeout("connect timed out after 8 seconds")
    )
    adapter = ls.LivescoreAdapter()
    with pytest.raises(SourceUnavailable) as excinfo:
        adapter.fetch("football", "live", {})
    exc = excinfo.value
    assert exc.source == "livescore"
    assert exc.code == "source_unavailable"
    # detail quotes this build's probe evidence (2026-09-02 TCP timeouts)
    assert "2026-09-02" in exc.detail
    assert "TCP connect timed out" in exc.detail
    assert "curl -4" in exc.detail


def test_fetch_surfaces_transport_error_from_any_valid_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # every sport/category cell routes to the single probed URL and the same
    # typed outcome — no cell pretends to have a data path
    _install_fake_transport(
        monkeypatch, error=httpx.ConnectTimeout("connect timed out after 8 seconds")
    )
    adapter = ls.LivescoreAdapter()
    for sport in ("football", "basketball", "tennis"):
        for category in ("fixtures", "live", "results", "h2h"):
            with pytest.raises(SourceUnavailable) as excinfo:
                adapter.fetch(sport, category, {"date": "2026-09-02", "limit": "50"})
            assert excinfo.value.source == "livescore"


# --- fetch: future-network status mapping (typed errors, never data) ----------


def test_fetch_403_raises_source_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_transport(monkeypatch, status=403)
    adapter = ls.LivescoreAdapter()
    with pytest.raises(SourceBlocked) as excinfo:
        adapter.fetch("football", "fixtures", {})
    assert excinfo.value.source == "livescore"
    assert excinfo.value.detail.startswith("HTTP 403")


def test_fetch_429_raises_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_transport(monkeypatch, status=429)
    adapter = ls.LivescoreAdapter()
    with pytest.raises(RateLimited) as excinfo:
        adapter.fetch("football", "live", {})
    assert excinfo.value.source == "livescore"
    assert excinfo.value.code == "rate_limited"


def test_fetch_5xx_after_retries_raises_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_transport(monkeypatch, status=503)
    adapter = ls.LivescoreAdapter()
    with pytest.raises(SourceUnavailable) as excinfo:
        adapter.fetch("football", "live", {})
    assert excinfo.value.source == "livescore"
    assert "HTTP 503" in excinfo.value.detail
    assert "retries exhausted" in excinfo.value.detail


def test_map_challenge_html_200_to_source_blocked() -> None:
    body = b"<!doctype html><html><body>Just a moment... checking your browser Cloudflare</body></html>"
    with pytest.raises(SourceBlocked) as excinfo:
        ls._map_http_outcome(URL, 200, body, FETCHED_AT, "livescore")
    assert "challenge" in excinfo.value.detail


def test_map_unexpected_status_raises_source_unavailable() -> None:
    # 404 was never observed for this URL; nothing but the homepage was ever
    # probed, so an unknown status is evidence of a changed network, not a
    # data answer
    with pytest.raises(SourceUnavailable) as excinfo:
        ls._map_http_outcome(URL, 404, b"", FETCHED_AT, "livescore")
    assert excinfo.value.source == "livescore"
    assert "re-probe" in excinfo.value.detail


def test_map_plain_200_passes_raw_response_parse_rejects_it() -> None:
    # a genuine (non-challenge) 200 has never been observed; if one ever
    # arrives it passes through raw — parse_events must still refuse it
    resp = ls._map_http_outcome(URL, 200, b"<html>homepage</html>", FETCHED_AT, "livescore")
    assert isinstance(resp, SourceResponse)
    assert resp.status == 200
    assert resp.source == "livescore"


# --- parse_events: no contract exists, so no events can ever be returned ------


def test_parse_events_raises_no_data_on_any_input(adapter: ls.LivescoreAdapter) -> None:
    for payload in (
        b"<html>homepage</html>",
        b"{}",
        b"[]",
        b"",
        "{}",
    ):
        resp = SourceResponse(source="livescore", payload=payload, url=URL, status=200, fetched_at=FETCHED_AT)
        with pytest.raises(NoData) as excinfo:
            adapter.parse_events(resp)
        assert excinfo.value.source == "livescore"
        assert "no livescore parse contract exists" in excinfo.value.detail
        assert "2026-09-02" in excinfo.value.detail


# --- input validation (pre-network typed errors) ------------------------------


def test_fetch_unknown_sport_or_category_raises_bad_request(
    adapter: ls.LivescoreAdapter,
) -> None:
    # no fake transport installed: validation must raise BEFORE any request
    with pytest.raises(BadRequest):
        adapter.fetch("handball", "live", {})
    with pytest.raises(BadRequest):
        adapter.fetch("football", "standings", {})
