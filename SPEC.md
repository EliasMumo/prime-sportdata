# prime-sportdata — SPEC (living document)

On-demand sports data scraper feeding the prediction model in `/home/flore/primepredict_algo`.
Model requests data over HTTP; this service scrapes then (flashscore / sofascore / livescore),
normalizes, and returns one envelope per request. Football, basketball, tennis.

## Decisions locked with the user (2026-09-02)

1. **Interface**: local FastAPI HTTP service in this folder. Model calls it on demand.
2. **Fetch stack**: local free stack — httpx + Scrapling(optional extra), **no Firecrawl, no cloud,
   no paid APIs** (matches the algo's free-tier constraint).
3. **Algo wiring**: build standalone here + integration doc. **Do not edit
   `/home/flore/primepredict_algo` in any stage.**

## Network evidence gathered 2026-09-02 (machine: Kali, /home/flore)

| Source | Probe result | Consequence |
|---|---|---|
| `www.flashscore.com/football/` (browser UA) | **HTTP 200** | Currently the most reachable source; primary candidate for football fixtures. Data path (server-rendered HTML / embedded JSON / feed) **must be probed and evidenced**, not assumed. |
| `www.sofascore.com/api/v1/sport/football/scheduled-events/2026-09-02` | timeout on default (v6-only DNS), **HTTP 404 over `curl -4`** (Fastly A=151.101.175.52; 44 B JSON error = API origin alive) | API reachable **IPv4-only**. Correct path shapes must be re-probed. Adapter MUST force IPv4. |
| `api.sofascore.com` (alias) | HTTP 404 (reachable, wrong paths) | Secondary host to try in probe list. |
| `www.livescore.com/en/` | TCP timeout even with `curl -4` | Edge blocks this client. Ship adapter with typed `source_unavailable`, honest health doc. Revisit only if a data-path probe succeeds. |

Rules these impose on workers: **every URL shipped in code must have been verified by a live
fetch** (documented in `docs/source-health.md` with date + status + response shape) — or must fail
gracefully with a typed error surfaced to the caller. Never ship a code path that returns data from
an endpoint that was not actually hit and parsed successfully during this build. Never invent rows
to make a response "look successful": empty-or-error is honest, fabricated is not.

## Scope — this build (v1)

- Categories: `fixtures`, `live`, `results`, `h2h` for each sport. That is 12 catalog rows.
- Query params: `date` (YYYY-MM-DD, default today), `team` / `entity_a`+`entity_b` (names, resolved
  per source), `league` (optional free text), `limit` (default 50, max 200).
- Per-request **single source wins** (first success in configured order); cross-source entity-id
  unification is **out of scope** — ids returned are the answering source's ids.
- Engine behavior: cache → rate limit → failover across sources → envelope with provenance.
- Status/standings/odds/stats/form/lineups/injuries: **roadmap only**, documented in
  `docs/architecture.md` extension section. Schema must not close the door to them.
- No DB, no scheduler, no background jobs, no auth (localhost service).

## Ban-risk policy (user: "we cannot afford IP bans") — 2026-09-02

Single residential IP, scraping must never endanger it. This is a volume + breaker policy:

1. **Volume budget**: on-demand-only + cache means tens of requests/day spread over 3 hosts. Never
   sweep, never paginate aggressively, never fetch more than the model asked for.
2. **Circuit breaker (mandatory, engine-level)**: per-source state machine. Trip on 403, 429,
   challenge/block signatures, or 2 consecutive 5xx/timeouts → `open` for a cooldown (default 30 min,
   escalating to 4 h after repeated trips; configurable). While open, that source is skipped in
   failover and its state is surfaced in `/health`. Half-open probe after cooldown. Never retry into
   a block.
3. **Fingerprint hygiene**: one static, realistic browser UA per source (config); no random UA
   rotation (patternless rotation is a bot signal). No headless-browser work unless a live probe
   proves plain fetch cannot get the data — and even then prefer curl_cffi impersonation
   (Scrapling StaticFetcher) over Playwright; Playwright/stealthy only as last resort for JS-only
   walls.
4. **Cache-first**: fixtures are static-ish → long TTL (30 min default) collapses repeat asks into
   1 fetch/source/day at the model's daily cadence. `live` TTL short (30 s). Cache success only.
5. **Retry discipline**: exponential backoff with jitter, max 2 retries per attempt, only on
   timeouts/5xx — never on 403/429 (those trip the breaker instead).
6. **Official-first doctrine** (integration-guide content): football → primepredict_algo's existing
   free official API-Football keys first; scraping is fallback/complement for tennis, basketball,
   live, and anything official tiers lack.
7. Tooling decision: **crawl4ai not adopted** — Playwright-core, heavier, more fingerprintable than
   curl_cffi impersonation, extraction features aimed at content pages not structured feeds;
   decision reversible because fetch backends sit behind `sources/base.py`.

## Non-goals & prohibitions

- No edits, installs, or reads-for-editing inside `/home/flore/primepredict_algo`.
- No paid/cloud scraping (Firecrawl not included; hybrid option rejected by user).
- No commits/git init unless the user asks. No secrets in code or docs.
- Polite crawling: per-host min interval ≥ 1 s (rate limiter in core), sequential probes,
  abort a probe session after 2 consecutive 5xx/timeouts and record what was seen.
- No attempt to defeat auth or log in anywhere. Bypassing *bot checks on public pages* via
  header impersonation / IPv4 pinning is in scope; credential-based or CAPTCHA-solving
  automation is not.

## Architecture

```
src/prime_sportdata/
├── config.py          pydantic-settings (env prefix PRIME_SPORTDATA_)
├── models.py          normalized pydantic models + envelope + error body
├── catalog.py         sport × category matrix, params, source-order defaults
├── cache.py           disk cache: data/cache/<sha1>.json (gzip), TTL + freshness meta
├── rate_limit.py      per-host min-interval limiter
├── errors.py          typed errors: SourceUnavailable, SourceBlocked, RateLimited,
│                      NotFound, NoData, BadRequest, DependencyMissing
├── sources/
│   ├── base.py        SourceAdapter ABC + SourceResponse + parse helpers
│   ├── sofascore.py   (IPv4-forced httpx; endpoints probed this build)
│   ├── flashscore.py  (primary target for football fixtures; probe-driven)
│   └── livescore.py   (graceful; expected blocked on this network)
├── engine.py          fetch_on_demand(sport, category, params) -> Envelope
└── server/
    ├── app.py         FastAPI factory (create_app(engine))
    └── routes.py      /health /catalog /v1/{sport}/{category} endpoints
```

### Normalized event shape (v1)

```
Event:
  sport: football|basketball|tennis
  external: {source, source_event_id, source_url}
  start_time_utc: ISO-8601 | null
  status: scheduled|live|finished|postponed|cancelled|interrupted
  home: {name, source_id|null, score: int|null, current_score: bool}
  away: {name, source_id|null, score: int|null, current_score: bool}
  score_lines: [{period_label: "H1"|"H2"|"FT"|"Q1".."OT"|"S1".., home, away}]   # optional
  competition: {name, country|null} | null
  live_detail: str | null    # e.g. "73'", "Q3 04:12", "2nd set"
  round_label: str | null    # tennis: stage; football: round if cheaply available
```

Tennis: `home`/`away` are players; sets are `score_lines`; `current_score` marks set-in-play where derivable.

### Envelope (every 200 response)

```json
{
  "data": { "events": [ ... ] } | { "events": [...], "home": {...}, "away": {...} },
  "meta": {
    "sport": "football", "category": "fixtures", "source": "flashscore",
    "cached": false, "fetched_at_utc": "...", "latency_ms": 123,
    "request": {"date": "2026-09-02", "league": null, "limit": 50},
    "warnings": []
  }
}
```
`h2h` payload: `{events:[...], home:{name, summary}, away:{name, summary}}` where summary is a
plain-language line derivable from the source (e.g. "W3 D1 L2 in last 6 vs {opponent}") — or omitted
when not derivable. `warnings[]` carries honesty notes (e.g. "endpoint shape unverified, parses may
break" — anything we ship despite imperfect knowledge MUST be flagged here, never silent).

### Errors (JSON, non-200)

```json
{"error": {"code": "source_unavailable", "detail": "...", "source": "livescore",
           "tried_sources": ["flashscore", "sofascore", "livescore"]}}
```
Mapping: 400 bad_request · 404 not_found / no_data → 404? → `no_data` with 404 is confusing; use
**404 only for unknown routes/sports/categories**, `no_data` → **204-like**? Simpler: no_data → HTTP 404
is fine for this service? No — a model polling a date with no matches must distinguish "bad query"
from "valid, nothing scheduled". Decide: valid-empty → **200 with `data.events: []`**; parse-failure
or no usable rows from an otherwise healthy source → `no_data` 502? Cleanest: source answered but
content unusable = `no_data` → HTTP **422**? Keep it boring: empty results are 200/[]; total failure
of all sources → **502** with code of the last error + `tried_sources`; blocked/rate-limited/5xx per
source are stepping stones to 502, not terminal. Single terminal error code per response; body above.

## Failover order (default, config-overridable)

| sport | category | order (best first) |
|---|---|---|
| football | fixtures/results/live | flashscore → sofascore → livescore |
| basketball/tennis | all | sofascore → flashscore → livescore |

Order rationale: flashscore HTML probed 200 and has all three sports; sofascore has a JSON API but
needs IPv4 forcing + re-probed paths; livescore currently unreachable. Engine must tolerate any
adapter raising, and the config must allow reordering without code change.

## API surface (v1)

- `GET /health` → `{"status":"ok","sources":{<source>:<ok|unavailable|blocked|untested>}}`
- `GET /catalog` → rows: sport × category + supported sources + params + example curl
- `GET /v1/{sport}/fixtures?date&team&league&limit`
- `GET /v1/{sport}/results?date&team&league&limit`
- `GET /v1/{sport}/live?league&limit`   (live ignores `date`)
- `GET /v1/{sport}/h2h?entity_a&entity_b&league`  (tennis: players; football/basketball: teams)

`team`/`entity_a|b` filters are advisory: source search resolves names; if resolution fails → 200
empty events + warning, never a wrong-team guess. Start uvicorn: `.venv/bin/uvicorn prime_sportdata.server.app:app --port 8097`.

## Quality gates (mirror the algo's bar)

- `ruff check --no-cache .` (line length 100)
- `mypy --cache-dir /tmp/pss_mypy_cache src` (strict-ish: `disallow_untyped_defs = true` in config;
  justified `# type: ignore` allowed on JSON-shape edges)
- `pytest -q` (unit; **no network** — adapters tested against recorded fixtures under
  `tests/fixtures/<source>/<sport>/...`)
- `pytest -q -m live` (opt-in real-network smoke; must be run at least once per adapter during this
  build and its outcome recorded in `docs/source-health.md`)

## Outputs of this build

Code as above + tests + `docs/architecture.md` (incl. roadmap & extension points) +
`docs/api-contract.md` (every endpoint with real curl examples, envelope/error examples, catalog
table) + `docs/integration-guide.md` (how primepredict_algo wires this in as an HTTP provider:
registry/health/circuit pointers to its AGENTS.md, TTL advice, phase-2 data needs → future
categories) + `docs/source-health.md` (real probe table, one row per source, dated).
