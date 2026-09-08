"""Application settings (pydantic-settings, env prefix PRIME_SPORTDATA_).

Cache/politeness/TTL knobs the core needs (SPEC Ban-risk policy §4); failover
order overrides stay a caller-supplied mapping into ``catalog.source_order``
(engine builds it from config, e.g. ``PRIME_SPORTDATA_ORDER_FOOTBALL_FIXTURES``).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for prime-sportdata.

    Populated from environment variables prefixed with ``PRIME_SPORTDATA_``
    (e.g. ``PRIME_SPORTDATA_LOG_LEVEL=debug``), falling back to defaults.
    """

    model_config = SettingsConfigDict(env_prefix="PRIME_SPORTDATA_")

    log_level: str = "info"
    # Optional bearer token.  Empty (default) keeps the service localhost-only
    # per SPEC.  When hosting publicly (e.g. Render), set a long random value
    # and the /v1 routes will require ``Authorization: Bearer <token>``.
    # /health and /catalog stay open for the readiness check and host probes.
    access_token: str = ""
    # Disk cache directory (SPEC: data/cache/<sha1(key)>.json.gz). Relative
    # paths resolve against the process CWD; the engine may absolutize.
    cache_dir: str = "data/cache"
    # Polite crawling: per-host minimum interval in seconds (SPEC >= 1 s).
    rate_limit_interval: float = 1.0
    # Ban-risk policy §4: fixtures are static-ish -> long TTL collapses repeat
    # asks; live is short-lived -> 30 s. Results/h2h buckets are not locked by
    # SPEC; the engine picks (results/h2h may reuse the fixtures bucket).
    cache_ttl_fixtures_seconds: int = 1800
    cache_ttl_live_seconds: int = 30
    # Betting quotes move faster than fixtures but slower than live scores;
    # 10 minutes keeps repeated model asks within one fetch while staying
    # honest about price staleness.
    cache_ttl_odds_seconds: int = 600
    # Ban-risk policy §2: per-source circuit breaker defaults (engine-level,
    # in-memory). Cooldown doubles per repeated trip from the base toward the
    # max (30 min -> 1 h -> 2 h -> 4 h); recovery resets the level. A source
    # opens on one 403/429/challenge or on `breaker_trip_after_consecutive`
    # consecutive 5xx/timeout-class failures; while open it is skipped in
    # failover and surfaced in /health. Half-open probe after the cooldown.
    breaker_enabled: bool = True
    breaker_cooldown_seconds: int = 1800
    breaker_cooldown_max_seconds: int = 14400
    breaker_trip_after_consecutive: int = 2
