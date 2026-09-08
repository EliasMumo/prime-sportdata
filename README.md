# prime-sportdata

On-demand sports data scraper (football, basketball, tennis) that feeds the
prediction model in `/home/flore/primepredict_algo` over local HTTP: the model
requests data, this service scrapes (flashscore / sofascore / livescore),
normalizes, and returns one envelope per request.

Local free stack only — httpx + FastAPI; no cloud, no paid APIs, no headless
browsers (SPEC §2, §7). Requires Python >= 3.12.

## Quickstart

```bash
cd /home/flore/primesportdata
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,server]"   # server = fastapi + uvicorn; research = scrapling (optional, inactive)
```

Start the service (from the repo root, so the `data/cache` default resolves
under the repo):

```bash
.venv/bin/uvicorn prime_sportdata.server.app:app --port 8097
```

Try it:

```bash
curl -s http://127.0.0.1:8097/health
curl -s http://127.0.0.1:8097/catalog
curl -s 'http://127.0.0.1:8097/v1/football/fixtures?limit=3'
```

The `/v1` data calls are the only ones that may hit real sports sources
(one per request-key, cache-first, rate-limited per host, per-source circuit
breakers — see the SPEC Ban-risk policy). Deterministic error paths do
not touch the network: `/v1/cricket/fixtures` → 404, `?limit=500` → 400.

## Tests & checks

```bash
.venv/bin/python -m pytest -q            # offline unit suite (no network); live tests opt-in via -m live
.venv/bin/python -m pytest -q -m live    # opt-in real-network smoke (probe outcomes in docs/source-health.md)
.venv/bin/ruff check --no-cache .
.venv/bin/mypy --cache-dir /tmp/pss_mypy_cache src
```

Latest offline gate: 142 passed / 9 deselected; ruff + mypy clean (16 source
files).

## Configuration

pydantic-settings, env prefix `PRIME_SPORTDATA_` — e.g.
`PRIME_SPORTDATA_LOG_LEVEL=debug`,
`PRIME_SPORTDATA_RATE_LIMIT_INTERVAL=1.5`. Defaults live in
`src/prime_sportdata/config.py`: cache TTL 1800 s (fixtures/results/h2h) /
30 s (live), per-host rate-limit interval 1.0 s, circuit breaker cooldown
1800 s doubling to 14400 s cap, trip after 2 consecutive 5xx/timeout-class
failures (or one 403/429/challenge).

## Docs

- `docs/architecture.md` — components, request pipeline, circuit-breaker state
  machine, error mapping, 12-cell coverage matrix, roadmap & extension points.
- `docs/api-contract.md` — every endpoint with params, validation rules,
  example curls and captured live output (envelope + error bodies).
- `docs/integration-guide.md` — how primepredict_algo wires this in as an HTTP
  provider (health polling, TTL advice, error handling, official-first
  doctrine, per-source caveats).
- `docs/source-health.md` — the dated live-probe evidence per source: which
  endpoints returned what on 2026-09-02, and which cells ship typed errors.
- `SPEC.md` / `STAGE_MAP.md` — build specification and stage map.

## Layout

```
src/prime_sportdata/
├── config.py / models.py / catalog.py / cache.py / rate_limit.py / errors.py
├── sources/        # base.py (adapter contract) + flashscore/sofascore/livescore adapters
├── engine.py       # cache -> rate limit -> breakers -> failover -> envelope
└── server/         # app.py (FastAPI factory) + routes.py (/health /catalog /v1/...)
tests/              # offline unit tests + recorded source fixtures under tests/fixtures/
```

## Odds (2026-09-08)

Two odds categories serve the prediction model's fallback:

- `GET /v1/{sport}/odds` — BetExplorer 1x2 / two-way markets (all three sports).
- `GET /v1/football/odds_detailed` — Betika half-time/full-time (all nine
  outcomes), correct score (0:0–4:4 + OTHER), 2.5 totals and double chance.

All quotes are **observed market evidence**: `bookmaker` records the display
column/site, and nothing is ever renamed into an approved execution
bookmaker. Evidence in `docs/source-health.md`.

## Hosting on Render

`render.yaml` is a ready blueprint. Deploy from the Render dashboard (New ->
Blueprint) and set `PRIME_SPORTDATA_ACCESS_TOKEN` to a random value
(`openssl rand -hex 32`). On the hosted instance `/v1/*` then requires
`Authorization: Bearer <token>`; `/health` and `/catalog` stay open for
probes. Point the algo at it with:

```
SPORTDATA_ODDS_BASE_URL=https://<your-app>.onrender.com
SPORTDATA_ODDS_TOKEN=<same token>
ENABLE_SPORTDATA_ODDS=true
```

Local development stays token-free (empty `PRIME_SPORTDATA_ACCESS_TOKEN`).
