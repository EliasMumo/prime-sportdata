"""Base adapter contract tests: SourceResponse, parse helpers, typed raises."""

import pytest

from prime_sportdata.errors import NoData, PrimeSportDataError, SourceBlocked, SourceUnavailable
from prime_sportdata.models import Event, ExternalRef
from prime_sportdata.sources.base import (
    ParseOutcome,
    SourceAdapter,
    SourceResponse,
    parse_json_response,
    parse_warning,
    stamp_provenance,
)

URL = "https://www.flashscore.com/football/match/abc123/"


def make_response(payload=None, source="flashscore", status=200):
    return SourceResponse(
        source=source,
        payload=payload if payload is not None else {"events": []},
        url=URL,
        status=status,
        fetched_at="2026-09-02T17:00:00Z",
    )


def test_source_response_fields():
    resp = make_response(payload=b"raw", status=200)
    assert resp.source == "flashscore"
    assert resp.payload == b"raw"
    assert resp.status == 200
    assert resp.url == URL
    assert resp.fetched_at == "2026-09-02T17:00:00Z"


def test_parse_json_response_dict_passthrough_and_decoding():
    assert parse_json_response(make_response({"a": 1})) == {"a": 1}
    assert parse_json_response(make_response(payload=b'{"a": 1}')) == {"a": 1}
    assert parse_json_response(make_response(payload='{"a": 1}')) == {"a": 1}
    assert parse_json_response(make_response(payload='[1, 2]')) == [1, 2]


def test_parse_json_response_undecodable_raises_no_data_with_source():
    with pytest.raises(NoData) as excinfo:
        parse_json_response(make_response(payload=b"<html>challenge</html>"))
    assert excinfo.value.source == "flashscore"
    assert excinfo.value.detail
    assert isinstance(excinfo.value, PrimeSportDataError)


def test_parse_warning_follows_spec_honesty_convention():
    resp = make_response()
    warning = parse_warning("endpoint shape unverified - parses may break", resp)
    assert "endpoint shape unverified" in warning
    assert "flashscore" in warning
    assert URL in warning


def test_stamp_provenance_anchors_events_to_real_response():
    resp = make_response()
    ev_missing_url = Event(
        sport="football",
        external=ExternalRef(source="flashscore", source_event_id="e1"),
        status="scheduled",
        home={"name": "A"},
        away={"name": "B"},
    )
    ev_wrong_source = Event(
        sport="football",
        external=ExternalRef(
            source="elsewhere", source_event_id="e2", source_url="https://elsewhere/x"
        ),
        status="scheduled",
        home={"name": "A"},
        away={"name": "B"},
    )
    anchored = stamp_provenance(resp, [ev_missing_url, ev_wrong_source])
    assert anchored[0].external.source == "flashscore"
    assert anchored[0].external.source_url == URL  # fell back to the fetched URL
    assert anchored[0].external.source_event_id == "e1"
    assert anchored[1].external.source == "flashscore"  # corrected, not trusted
    assert anchored[1].external.source_url == "https://elsewhere/x"  # real one kept
    # Parser may keep calling it with the already-anchored event unchanged.
    again = stamp_provenance(resp, anchored)
    assert again[0] is anchored[0]


def test_adapter_contract_with_fake_adapter():
    class FakeAdapter(SourceAdapter):
        source = "fake"

        def fetch(self, sport, category, params):
            assert sport == "football" and category == "fixtures"
            assert params["date"] == "2026-09-02"
            return SourceResponse(
                source=self.source,
                payload={"events": []},
                url=URL,
                status=200,
                fetched_at="2026-09-02T17:00:00Z",
            )

        def parse_events(self, resp):
            return ParseOutcome(events=[], warnings=["unverified"])

    adapter = FakeAdapter()
    assert isinstance(adapter, SourceAdapter)
    resp = adapter.fetch("football", "fixtures", {"date": "2026-09-02"})
    assert isinstance(resp, SourceResponse)
    outcome = adapter.parse_events(resp)
    assert isinstance(outcome, ParseOutcome)
    assert outcome.warnings == ["unverified"]


def test_typed_errors_cross_adapter_boundary():
    class BlockedAdapter(SourceAdapter):
        source = "blocked"

        def fetch(self, sport, category, params):
            raise SourceBlocked("403 from WAF", source=self.source)

        def parse_events(self, resp):
            raise NoData("nothing usable", source=resp.source)

    adapter = BlockedAdapter()
    with pytest.raises(SourceBlocked) as excinfo:
        adapter.fetch("football", "fixtures", {})
    assert excinfo.value.source == "blocked"
    with pytest.raises(NoData):
        adapter.parse_events(make_response())


def test_source_unavailable_carries_source_for_failover_trace():
    err = SourceUnavailable("TCP timeout after connect", source="livescore")
    assert err.source == "livescore"
    assert err.detail == "TCP timeout after connect"
