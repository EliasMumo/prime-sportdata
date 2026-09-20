"""HTTP API surface (SPEC "API surface"): /health, /catalog, /v1/{sport}/{category}.

Validation rules (SPEC + stage-5 contract):

* ``GET /health`` -> 200 ``{"status": "ok", "sources": {...}, "breakers": {...}}``.
  ``sources`` maps each source to ``ok|unavailable|blocked|untested`` (SPEC);
  ``breakers`` carries the per-source breaker states (state machine snapshot).
* ``GET /catalog`` -> the 12+ sport x category rows with supported sources,
  accepted params and an example curl per row.
* ``GET /v1/{sport}/{category}`` -> one Envelope (200) or a SPEC ErrorBody.
  Unknown sport/category -> 404 ``not_found``; invalid param values (date not
  YYYY-MM-DD, limit not an int / out of 1..200) -> 400 ``bad_request``; h2h
  without both entities -> 400; all sources failed -> 502 with the code of the
  LAST error + ``tried_sources`` (SPEC "Errors": valid-empty is 200/[] — never
  an error; blocked/rate-limited/5xx per source are stepping stones to the
  502, never terminal on their own).

Every response body matches ``models.py`` shapes (Envelope / ErrorBody). The
engine is resolved lazily through ``request.app.state.engine_provider`` so a
TestClient can inject one built on fake adapters.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from prime_sportdata.catalog import (
    LIMIT_DEFAULT,
    LIMIT_MAX,
    ROWS,
    SOURCES,
    CatalogRow,
    get_row,
)
from prime_sportdata.engine import Engine, FetchFailed
from prime_sportdata.errors import PrimeSportDataError
from prime_sportdata.models import Category, ErrorBody, ErrorDetail, Sport

router = APIRouter()

# Base URL used in catalog example curls (SPEC uvicorn command, port 8097).
BASE_URL = "http://127.0.0.1:8097"


def _engine(request: Request) -> Engine:
    return request.app.state.engine_provider.get()


def _error(status: int, code: str, detail: str, source: str | None = None) -> JSONResponse:
    body = ErrorBody(error=ErrorDetail(code=code, detail=detail, source=source))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _is_iso_date(value: str) -> bool:
    """Strict YYYY-MM-DD check incl. calendar validity (aware datetime, no
    naive constructions): '2026-02-30', '2026-9-2' and '2026-13-01' are out."""
    year, sep, rest = value.partition("-")
    if not sep or "-" not in rest:
        return False
    month, sep2, day = rest.partition("-")
    if not sep2 or len(year) != 4 or len(month) != 2 or len(day) != 2:
        return False
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return False
    try:
        datetime(int(year), int(month), int(day), tzinfo=UTC)
    except ValueError:
        return False
    return True


def _example_curl(row_sport: str, row_category: str, params: tuple[str, ...]) -> str:
    """Per-row example curl with the params that row actually accepts."""
    qs = ["date=2026-09-02"] if "date" in params and row_category != "live" else []
    if "team" in params:
        qs.append("team=Arsenal")
    if "entity_a" in params:
        qs.append("entity_a=Team+A")
    if "entity_b" in params:
        qs.append("entity_b=Team+B")
    if "team_a" in params:
        qs.append("team_a=Arsenal")
    if "team_b" in params:
        qs.append("team_b=Chelsea")
    if "league" in params:
        qs.append("league=Premier+League")
    qs.append(f"limit={LIMIT_DEFAULT}")
    return f"curl -s '{BASE_URL}/v1/{row_sport}/{row_category}?" + "&".join(qs) + "'"


@router.get("/health")
def health(request: Request) -> JSONResponse:
    engine = _engine(request)
    views = engine.breaker_views()
    return JSONResponse(
        content={
            "status": "ok",
            "sources": {name: view.status for name, view in views.items()},
            "breakers": {name: asdict(view) for name, view in views.items()},
        }
    )


@router.get("/catalog")
def catalog() -> JSONResponse:
    rows = [
        {
            "sport": row.sport,
            "category": row.category,
            "params": list(row.params),
            "sources": list(row.sources_default),
            "limit_default": row.limit_default,
            "limit_max": row.limit_max,
            "example_curl": _example_curl(row.sport, row.category, row.params),
        }
        for row in ROWS
    ]
    return JSONResponse(content=rows)


def _parse_row(sport: str, category: str) -> tuple[CatalogRow | None, JSONResponse | None]:
    """Resolve (sport, category) to its catalog row; 404 ErrorBody otherwise."""
    try:
        row = get_row(cast(Sport, sport), cast(Category, category))
    except KeyError:
        return None, _error(
            404,
            "not_found",
            f"unknown sport or category: /v1/{sport}/{category} "
            "(known sports: football, basketball, tennis; categories: "
            "fixtures, live, results, h2h, odds, odds_detailed, odds_linebet, lineups)",
        )
    return row, None


def _parse_query(
    request: Request,
    sport: str,
    category: str,
    row_params: tuple[str, ...],
) -> tuple[dict[str, str] | None, JSONResponse | None]:
    """Validate query params per the catalog row; (params, None) or (None, 400)."""
    raw = request.query_params
    params: dict[str, str] = {}
    for key in row_params:
        if key in raw:
            params[key] = raw[key]
    # h2h: both entities are required (else 400 bad_request).
    if category == "h2h":
        missing = [k for k in ("entity_a", "entity_b") if not params.get(k, "").strip()]
        if missing:
            return None, _error(
                400, "bad_request", f"h2h requires both entities: {', '.join(missing)} missing"
            )
    # lineups: both team names are required (resolution is search -> shared
    # upcoming event -> sheets; no verified name-free path).
    if category == "lineups":
        missing = [k for k in ("team_a", "team_b") if not params.get(k, "").strip()]
        if missing:
            return None, _error(
                400, "bad_request", f"lineups requires both teams: {', '.join(missing)} missing"
            )
    # date: default today; must be YYYY-MM-DD when given (fixtures/results only;
    # live ignores date by construction — LIVE_PARAMS has no date key).
    if "date" in row_params:
        raw_date = params.get("date")
        if raw_date is None:
            params["date"] = _today_iso()
        elif not _is_iso_date(raw_date):
            return None, _error(400, "bad_request", f"date must be YYYY-MM-DD, got {raw_date!r}")
    # limit: default 50; must parse as 1..LIMIT_MAX (SPEC: >LIMIT_MAX -> 400).
    raw_limit = params.get("limit")
    if raw_limit is None:
        params["limit"] = str(LIMIT_DEFAULT)
    else:
        try:
            limit = int(raw_limit)
        except ValueError:
            return None, _error(400, "bad_request", f"limit must be an integer, got {raw_limit!r}")
        if not 1 <= limit <= LIMIT_MAX:
            return None, _error(
                400, "bad_request", f"limit must be between 1 and {LIMIT_MAX}, got {raw_limit!r}"
            )
    return params, None


@router.get("/v1/{sport}/{category}")
def v1(request: Request, sport: str, category: str) -> JSONResponse:
    row, err = _parse_row(sport, category)
    if row is None:
        return err if err is not None else _error(404, "not_found", "unknown sport or category")
    params, err = _parse_query(request, sport, category, row.params)
    if params is None:
        return err if err is not None else _error(400, "bad_request", "invalid query parameters")
    # Optional single-source pinning (caller-trusted opt-in): answers the
    # request from exactly one source instead of the catalog failover order.
    # Used for targeted lookups (e.g. sofascore team-scoped results when the
    # primary day table cannot help). Unknown keys like ``source`` are already
    # dropped by ``_parse_query`` so adapters never see them.
    source: str | None = request.query_params.get("source")
    if source is not None:
        source = source.strip()
        if not source or source not in SOURCES:
            return _error(
                400,
                "bad_request",
                f"unknown source {source!r} (known sources: {', '.join(SOURCES)})",
            )
    engine = _engine(request)
    try:
        envelope = engine.fetch_on_demand(
            str(sport), str(category), params, source=source
        )
    except FetchFailed as exc:
        # SPEC "Errors": 502 with the code of the LAST error + tried_sources.
        body = ErrorBody(
            error=ErrorDetail(
                code=exc.last_code,
                detail=exc.detail,
                source=exc.source,
                tried_sources=exc.tried_sources,
            )
        )
        return JSONResponse(status_code=502, content=body.model_dump(mode="json"))
    except PrimeSportDataError as exc:
        # Defensive: engine-level typed errors are not expected through the
        # route (sport/category were validated above); single-code mapping.
        body = ErrorBody(error=ErrorDetail(code=exc.code, detail=exc.detail, source=exc.source))
        return JSONResponse(status_code=502, content=body.model_dump(mode="json"))
    return JSONResponse(content=envelope.model_dump(mode="json"))
