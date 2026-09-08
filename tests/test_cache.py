"""Disk cache tests: fake-clock TTL, gzip round-trip, corrupt-entry misses."""

import gzip
import hashlib
import json

from prime_sportdata.cache import DiskCache


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_set_get_round_trip_at_expected_path(tmp_path):
    clock = FakeClock()
    cache = DiskCache(cache_dir=tmp_path, clock=clock)
    payload = {"events": [{"sport": "football"}], "meta": {"ok": True}}
    cache.set("football:fixtures:2026-09-02", payload, ttl_seconds=30.0)

    expected = hashlib.sha1(b"football:fixtures:2026-09-02").hexdigest()
    path = tmp_path / f"{expected}.json.gz"
    assert path.exists()
    # gzip round trip: file content is gzip JSON with payload + freshness meta.
    raw = gzip.decompress(path.read_bytes())
    entry = json.loads(raw)
    assert entry["payload"] == payload
    assert entry["stored_at"] == 1000.0
    assert entry["ttl_seconds"] == 30.0

    got, fresh, fetched_at = cache.get("football:fixtures:2026-09-02")
    assert got == payload
    assert fresh is True
    assert fetched_at == 1000.0


def test_ttl_honored_with_fake_clock(tmp_path):
    clock = FakeClock()
    cache = DiskCache(cache_dir=tmp_path, clock=clock)
    cache.set("k", {"a": 1}, ttl_seconds=30.0)

    clock.advance(29.9)
    payload, fresh, _ = cache.get("k")
    assert payload == {"a": 1}
    assert fresh is True

    clock.advance(0.1)  # exactly at TTL boundary -> stale
    _, fresh, _ = cache.get("k")
    assert fresh is False

    # Stale payload is still returned (stale-if-error support), caller honors TTL.
    clock.advance(60.0)
    payload, fresh, fetched_at = cache.get("k")
    assert payload == {"a": 1}
    assert fresh is False
    assert fetched_at == 1000.0


def test_miss_returns_none_never_raises(tmp_path):
    cache = DiskCache(cache_dir=tmp_path, clock=FakeClock())
    assert cache.get("absent-key") == (None, False, None)


def test_entries_survive_new_instance_and_overwrite(tmp_path):
    clock = FakeClock()
    DiskCache(cache_dir=tmp_path, clock=clock).set("k", {"v": 1}, ttl_seconds=30.0)

    second = DiskCache(cache_dir=tmp_path, clock=FakeClock(start=1005.0))
    payload, fresh, fetched_at = second.get("k")
    assert payload == {"v": 1}
    assert fresh is True
    assert fetched_at == 1000.0

    second.set("k", {"v": 2}, ttl_seconds=30.0)
    payload, _, fetched_at = second.get("k")
    assert payload == {"v": 2}
    assert fetched_at == 1005.0


def test_per_key_isolation(tmp_path):
    cache = DiskCache(cache_dir=tmp_path, clock=FakeClock())
    cache.set("one", {"n": 1}, ttl_seconds=30.0)
    assert cache.get("two") == (None, False, None)
    assert cache.get("one")[0] == {"n": 1}


def test_corrupt_entries_return_miss(tmp_path):
    cache = DiskCache(cache_dir=tmp_path, clock=FakeClock())
    cache.set("k", {"v": 1}, ttl_seconds=30.0)
    path = cache._key_path("k")

    # Truncated gzip / not gzip at all.
    path.write_bytes(b"not gzip at all")
    assert cache.get("k") == (None, False, None)

    # Valid gzip, not JSON.
    path.write_bytes(gzip.compress(b"<html>not json</html>"))
    assert cache.get("k") == (None, False, None)

    # Valid JSON, not the expected envelope shape (no payload field).
    path.write_bytes(gzip.compress(json.dumps({"unexpected": 1}).encode("utf-8")))
    assert cache.get("k") == (None, False, None)

    # Valid JSON with a non-dict payload.
    path.write_bytes(gzip.compress(b'{"payload": [1, 2], "stored_at": 1.0, "ttl_seconds": 30.0}'))
    assert cache.get("k") == (None, False, None)

    # A different key is untouched.
    cache.set("other", {"v": 9}, ttl_seconds=30.0)
    assert cache.get("other")[0] == {"v": 9}


def test_set_creates_cache_dir_and_no_tmp_leftovers(tmp_path):
    cache_dir = tmp_path / "nested" / "cache"
    cache = DiskCache(cache_dir=cache_dir, clock=FakeClock())
    cache.set("k", {"v": 1}, ttl_seconds=30.0)
    assert cache_dir.is_dir()
    assert list(cache_dir.glob("*.tmp*")) == []
    assert list(cache_dir.glob("*.json.gz")) == [cache._key_path("k")]
