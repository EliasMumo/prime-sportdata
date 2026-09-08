# STAGE_MAP — prime-sportdata build (living document; orchestrator appends status lines)

Replan budget: at most 2 replans. Beyond that → stop and ask the user. Workers never spawn workers.

## Stage 1 — Scaffold & installability
**Artifact**: `pyproject.toml` (project `prime-sportdata`, package `prime_sportdata`, py>=3.12;
deps: httpx, pydantic>=2, pydantic-settings; extras: `server`=[fastapi, uvicorn], `research`=[scrapling],
`dev`=[ruff, mypy, pytest, pytest-asyncio, fastapi]); src package importable; `.gitignore`; `README.md` stub;
minimal `config.py`; `tests/` with conftest.
**Check (failable)**: `.venv/bin/pip install -e ".[dev,server]"` exits 0 AND `.venv/bin/python -c "import prime_sportdata"` succeeds. Ruff/mypy present in venv.
**Worker**: fable-worker-haiku. Do NOT git init.

## Stage 2 — Core: models, errors, cache, rate_limit, catalog, adapter base
**Artifact**: `models.py` (Event/Envelope/error body per SPEC), `errors.py`, `cache.py` (disk, gzip,
sha1 key, TTL, fake-clock testable), `rate_limit.py` (per-host min interval), `catalog.py` (12 rows +
source-order defaults per SPEC), `sources/base.py` (ABC, SourceResponse, typed raises), unit tests.
**Check**: `pytest -q` green offline; `ruff check --no-cache .` and `mypy --cache-dir /tmp/pss_mypy_cache src` clean on src.
**Worker**: fable-worker-sonnet.

## Stage 3 — Sofascore adapter (probe-driven)
**Artifact**: `sources/sofascore.py` covering football/basketball/tennis × fixtures/live/results/h2h
as reachable in practice. MUST force IPv4 (evidence: v6 blackhole on this network). Probe candidate
paths live (sequential, ≥1 s apart, abort after 2 consecutive failures), correct as needed, record
fixtures under `tests/fixtures/sofascore/`, ship only verified endpoints — others raise typed errors.
**Check**: offline `pytest -q` green; live probe outcome recorded in `docs/source-health.md` (dated row:
which endpoints returned what, response shape snippet); adapter either returns real parsed data in a
`-m live` run **or** cleanly raises typed errors with evidence of what was tried. A 404-from-wrong-path
is evidence, not failure of the stage — but the doc must say exactly what works and what does not.
**Worker**: fable-worker-sonnet.

## Stage 4 — Flashscore + Livescore adapters (probe-driven)
**Artifact**: `sources/flashscore.py` (football primary target — plain-UA HTML returned 200 this build;
find the data path: server-rendered HTML, embedded JSON state, or feed endpoint; Scrapling only via
`research` extra, lazy-imported, `DependencyMissing` when absent) and `sources/livescore.py`
(expected: typed `source_unavailable` — edge blocked at probe time; adapter still structured per base
contract). Recorded fixtures + unit tests both.
**Check**: offline `pytest -q` green (incl. flashscore-from-fixture parse test and
flashscore-without-scrapling → `DependencyMissing` test); live probe rows for both sources in
`docs/source-health.md`, honest about what worked (flashscore: expected real data; livescore:
expected blocked, evidence of the timeout shown).
**Worker**: fable-worker-sonnet (flashscore is the reasoning-heavy one); optionally fable-worker-haiku
parallel for livescore — orchestrator's call, ≤2 concurrent.

## Stage 5 — Engine failover + circuit breaker + FastAPI server
**Artifact**: `engine.py` (cache → rate limit → circuit breaker → per-sport/category ordered failover →
envelope; valid-empty = 200 [] ; all-fail = 502 with code/tried_sources; no fabricated rows) with a
per-source breaker per SPEC "Ban-risk policy" §2 (trip on 403/429/block sig/2×5xx; cooldown 30 min →
4 h escalation; half-open probe; surfaced in /health) + `server/app.py` + `routes.py` (/health
/catalog /v1/{sport}/fixtures|results|live|h2h per SPEC, TestClient-friendly dependency injection of
engine). Engine unit tests with fake adapters: A-down→B-used; A-up→B-untouched; all-down→502 shape;
cache-hit→no fetch; TTL honored; rate limiter serializes; breaker trips on 403 → A skipped during
cooldown while B answers; breaker does not trip on healthy responses.
**Check**: `pytest -q` green; TestClient: /health 200 (and reports breaker state), /catalog lists
≥12 rows, unknown sport → 404 error body; ruff+mypy clean.
**Worker**: fable-worker-sonnet.

## Stage 6 — Docs + cold verification pass
**Artifact**: `docs/architecture.md` (roadmap/extension points), `docs/api-contract.md` (endpoint +
curl examples + catalog table), `docs/integration-guide.md` (primepredict_algo wiring per its
AGENTS.md provider/health/circuit model, TTL advice, phase-2 data needs, official-first doctrine:
football → the algo's existing free API-Football keys first, scraping as fallback/complement),
`docs/source-health.md`
(consolidated probe table), README quickstart.
**Check**: fable-verifier (cold, spec-only brief) re-runs: install-import; pytest; ruff/mypy;
catalog ≥12 rows via TestClient; every endpoint named in api-contract.md exists in the route table
(grep); README quickstart commands are real paths. Verifier returns pass/fail per check — no fixes
from verifier; fixes go back to the owning stage worker.
**Worker**: fable-worker-sonnet for docs; **fable-verifier** for the cold pass.

## Orchestrator rules
- Read SPEC.md + this map first. Maintain `WORKLOG.md` (append per stage: started/status/check
  result/evidence lines). Re-run invalidated checks after any fix.
- Every artifact comes from a named worker. ≤2 workers concurrent. Workers get: task, exact output
  path(s), SPEC pointers, named pass condition. Workers don't spawn workers.
- Never edit `/home/flore/primepredict_algo`. No git init. No secrets. Politeness limits per SPEC.
- A stage check that cannot pass after honest effort → log evidence, mark stage partial, adjust the
  map (≤2 replans), never fake a pass.
- Final report: per stage — artifact path, check name, result; plus one-line source-health summary.
