"""Live-network smoke test for the livescore adapter (opt-in: -m live).

Exactly one real request through the shipped adapter. The 2026-09-02 evidence
(SPEC network table + this build's re-probe) says www.livescore.com
TCP-times-out from this network, so the expected outcome is a typed
``SourceUnavailable`` — this test PASSES by receiving that typed error, which
IS the adapter's verified behavior this build (no data path exists to assert
anything else). Outcome of this file's run is recorded in
docs/source-health.md. Sequential, >= 1.3 s spacing, tiny volume (SPEC
Ban-risk policy).
"""

import time

import pytest

from prime_sportdata.errors import SourceUnavailable
from prime_sportdata.sources.livescore import LivescoreAdapter

_MARK = pytest.mark.live
_SPACING_S = 1.3


def _spaced() -> None:
    time.sleep(_SPACING_S)


@_MARK
def test_live_fetch_raises_typed_source_unavailable() -> None:
    _spaced()
    adapter = LivescoreAdapter()
    with pytest.raises(SourceUnavailable) as excinfo:
        adapter.fetch("football", "live", {})
    exc = excinfo.value
    assert exc.source == "livescore"
    assert exc.code == "source_unavailable"
    assert "2026-09-02" in exc.detail  # probe evidence quoted in the detail
    assert "TCP connect timed out" in exc.detail
    print(f"livescore fetch outcome: {exc.code} after retry discipline ({exc.detail[:120]}...)")
