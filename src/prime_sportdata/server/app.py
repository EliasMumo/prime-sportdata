"""FastAPI application factory (SPEC architecture: ``server/app.py``).

``create_app(engine=None)`` is the TestClient-friendly entry point: tests pass
an engine built on fake adapters (dependency injection). With no argument the
real engine is built from Settings/adapters — lazily, on the FIRST request, so
importing this module (e.g. from an offline test suite that never touches the
real engine path) never creates the real ``data/cache`` directory.

uvicorn target (SPEC): ``prime_sportdata.server.app:app`` — the module-level
``app`` below resolves its engine only when a request actually arrives.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from prime_sportdata.cache import DiskCache
from prime_sportdata.config import Settings
from prime_sportdata.engine import Engine
from prime_sportdata.rate_limit import RateLimiter
from prime_sportdata.server import routes
from prime_sportdata.sources.betexplorer import BetexplorerAdapter
from prime_sportdata.sources.betika import BetikaAdapter
from prime_sportdata.sources.flashscore import FlashscoreAdapter
from prime_sportdata.sources.linebet import LinebetAdapter
from prime_sportdata.sources.livescore import LivescoreAdapter
from prime_sportdata.sources.sofascore import SofascoreAdapter


class _EngineProvider:
    """Holds the injected engine or builds the default one on first use."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    def get(self) -> Engine:
        if self._engine is None:
            self._engine = _build_default_engine()
        return self._engine


def _build_default_engine() -> Engine:
    """Real engine from Settings + the shipped adapters (no network at build)."""
    settings = Settings()
    cache_dir = Path(settings.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = Path.cwd() / cache_dir
    return Engine(
        adapters={
            "flashscore": FlashscoreAdapter(),
            "sofascore": SofascoreAdapter(),
            "livescore": LivescoreAdapter(),
            "betexplorer": BetexplorerAdapter(),
            "betika": BetikaAdapter(),
            "linebet": LinebetAdapter(),
        },
        cache=DiskCache(cache_dir),
        limiter=RateLimiter(interval=settings.rate_limit_interval),
        ttl_live_seconds=float(settings.cache_ttl_live_seconds),
        ttl_default_seconds=float(settings.cache_ttl_fixtures_seconds),
        ttl_odds_seconds=float(settings.cache_ttl_odds_seconds),
        breaker_enabled=settings.breaker_enabled,
        breaker_cooldown_seconds=float(settings.breaker_cooldown_seconds),
        breaker_cooldown_max_seconds=float(settings.breaker_cooldown_max_seconds),
        breaker_trip_after=settings.breaker_trip_after_consecutive,
    )


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build the service. ``engine`` injectable for tests (fake adapters)."""
    app = FastAPI(title="prime-sportdata", version="0.1.0")
    app.state.engine_provider = _EngineProvider(engine)
    app.include_router(routes.router)
    access_token = Settings().access_token
    if access_token:
        app.middleware("http")(_bearer_token_gate(access_token))
    return app


def _bearer_token_gate(token: str) -> Callable[[Request, Callable[..., Awaitable[Response]]], Awaitable[Response]]:
    """Optional gate: /v1 requires ``Authorization: Bearer <token>`` when set.

    /health and /catalog stay open so the readiness check and host probes
    keep working on a hosted instance.  Installed only when
    ``PRIME_SPORTDATA_ACCESS_TOKEN`` is configured (empty = localhost mode).
    """

    async def gate(request: Request, call_next: Callable[..., Awaitable[Response]]) -> Response:
        if request.url.path.startswith("/v1"):
            header = request.headers.get("authorization") or ""
            if header != f"Bearer {token}":
                return JSONResponse(
                    status_code=401,
                    content={"error": {"code": "unauthorized", "detail": "missing bearer token"}},
                )
        return await call_next(request)

    return gate


# uvicorn target; engine resolves lazily on first request (see module docstring).
app = create_app()
