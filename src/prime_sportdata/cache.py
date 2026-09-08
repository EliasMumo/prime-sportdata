"""Disk cache at ``<cache_dir>/<sha1(key)>.json.gz`` (SPEC architecture tree).

Engine policy (cache success only, TTL per category from Settings per SPEC
Ban-risk policy §4) lives in the engine. This module only guarantees:

* storage layout ``<sha1(key)>.json.gz`` (gzip-compressed JSON),
* freshness metadata next to the payload (``stored_at`` + ``ttl_seconds``),
* atomic-ish writes (temp file + rename, no torn entries on crash),
* read failures (corrupt/truncated entries) surface as *misses*, never as
  exceptions to callers.

The clock is injectable so tests can advance time without sleeping.
"""

import gzip
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class DiskCache:
    """Files: ``<cache_dir>/<sha1(key)>.json.gz`` containing
    ``{"payload": ..., "stored_at": <clock()>, "ttl_seconds": ...}``.

    ``clock`` returns wall-clock seconds (float); time.time semantics so that
    freshness survives process restarts (monotonic would not).
    """

    def __init__(
        self,
        cache_dir: str | Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._clock = clock
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _key_path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json.gz"

    def set(self, key: str, payload: dict[str, Any], ttl_seconds: float) -> None:
        """Store ``payload`` under ``key`` with freshness metadata.

        Writes go to a temp file in the same directory and are renamed into
        place, so a crash never leaves a torn entry at the final path.
        """
        entry: dict[str, Any] = {
            "payload": payload,
            "stored_at": self._clock(),
            "ttl_seconds": ttl_seconds,
        }
        raw = gzip.compress(json.dumps(entry).encode("utf-8"))
        target = self._key_path(key)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=self._cache_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def get(self, key: str) -> tuple[dict[str, Any] | None, bool, float | None]:
        """Return ``(payload, fresh, stored_at)`` for ``key``.

        ``fresh`` is ``clock() - stored_at < ttl_seconds`` — the caller honors
        TTL. A stale payload is still returned (stale-if-error support); a
        miss or an unreadable/corrupt entry returns ``(None, False, None)``
        and never raises into the caller.
        """
        path = self._key_path(key)
        try:
            with gzip.open(path, "rb") as handle:
                raw = handle.read()
            entry = json.loads(raw.decode("utf-8"))
            payload = entry["payload"]
            if not isinstance(payload, dict):
                return None, False, None
            stored_at = float(entry["stored_at"])
            ttl_seconds = float(entry["ttl_seconds"])
            fresh = self._clock() - stored_at < ttl_seconds
            return payload, fresh, stored_at
        except (OSError, ValueError, KeyError, TypeError):
            # gzip.BadGzipFile / JSONDecodeError / UnicodeDecodeError all fold
            # into OSError/ValueError; missing or mistyped fields -> KeyError/
            # TypeError. Corrupt entries are misses, never raises.
            return None, False, None
