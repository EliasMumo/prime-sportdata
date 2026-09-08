# prime-sportdata ↔ primepredict_algo — integration guide

How the prediction model in `/home/flore/primepredict_algo` wires this service
in as a local HTTP data provider. Written for the algo's maintainers; per SPEC
decision 3 this build never edits `/home/flore/primepredict_algo` — the guide
is the deliverable, wiring lands in the algo's own Phase-2 work.

Facts about the algo cited below come from its `AGENTS.md`
(`/home/flore/primepredict_algo/AGENTS.md`, read-only reference) and from
reading its provider modules (`src/primepredict_algo/providers/*.py`). Facts
about this service come from its SPEC, code and `docs/source-health.md`
probe evidence.

## What primepredict_algo already has (read from its repo, 2026-09-02)

- **Provider registry with health/circuit/quota model** — `AGENTS.md`
  "Provider Health Model": score = `0.30*success + 0.25*completeness +
  0.15*freshness + 0.10*latency + 0.10*quota + 0.10*schema`; circuit states
  `closed → open → half_open`, opening on consecutive failures, high failure
  rate, quota exhaustion, 429s, schema < 0.70. `AGENTS.md` "Safety Rules":
  "All provider calls go through registry with health/circuit/quota checks";
  DB tables `provider_registry`, `provider_endpoint_health`,
  `provider_request_log`, `raw_cache` (gzip + SHA256 + TTL/stale-after).
  Code confirms: `providers/health.py` implements the same weights;
  `providers/circuit_breaker.py` opens on `quota_remaining <= 0`, repeated
  429, `schema_validity < 0.70`, `consecutive_failures >= 5`, or
  `failure_rate_24h >= 0.50` with ≥ 10 requests/24 h; cooldown default
  30 min; half-open probe after cooldown.
- **Registered providers** (`providers/registry.py::build_provider_registry`):
  `api_football` (API-Football), `football_data_org`, `thesportsdb`,
  `football_data_uk` for football; `api_basketball`, `balldontlie`,
  `api_tennis` (API-Sports family), `public_research`. Metadata carries
  `provider_id`, `sport`, `base_url`, `priority`, `enabled`, `free_tier_quota_policy`,
  `capabilities` (enum: `fixtures, results, odds, injuries, lineups, h2h,
  stats, historical, research` — note: **no `live` constant exists**),
  `key_present`. `ProviderEndpoint(key, path, cache_ttl_seconds,
  stale_after_seconds, essential)` is the endpoint contract; `ProviderAdapter
  .fetch` in Phase 1 raises `NotImplementedError("Real provider fetches begin
  in Phase 2")` — prime-sportdata is a natural Phase-2 HTTP provider.
- CLI surface: `primepredict-algo providers list`, `api-football-fixtures`,
  `football-data-matches`, `lookup-events`, `doctor`, `cache cleanup`, etc.
- Phase 2 needs (AGENTS.md Architecture Notes): "Real sports ingestion, odds,
  form, injury/lineup, calibration, simulation."

## The service being integrated (prime-sportdata)

- Local FastAPI service, base URL `http://127.0.0.1:8097` (SPEC). Start:
  `.venv/bin/uvicorn prime_sportdata.server.app:app --port 8097` from
  `/home/flore/primesportdata`.
- Three upstream sources behind one endpoint: `flashscore`, `sofascore`,
  `livescore` — per-request failover in the order each catalog row declares
  (`GET /catalog` returns it), so the model sees ONE envelope per request and
  never touches upstream hosts directly. All upstream hygiene (rate limit
  ≥ 1 s/host, one static UA, cache-first, breakers) lives here.
- API contract: `docs/api-contract.md` (param tables, bodies, captured curl
  outputs). Error taxonomy: `docs/architecture.md` "HTTP error mapping".

## Wiring recommendations

**1. Register as a provider entry.** Add prime-sportdata to
`build_provider_registry` with `provider_id` e.g. `prime_sportdata`,
`sport = "football"|"basketball"|"tennis"` per row (or one multi-sport entry
using the `sport` field of the metadata), `base_url = "http://127.0.0.1:8097"`,
`priority` above the scraping-tier priorities (below official keys for
football — see official-first doctrine below), `free_tier_quota_policy =
"local service; on-demand; cache-first"`, capabilities per row:
`FIXTURES, RESULTS` plus h2h/odds/... as roadmap cells land. Service-side
`/catalog` is the machine-readable authority for which (sport, category,
params) rows exist and which source order serves them.

**2. Endpoint definitions.** One `ProviderEndpoint` per catalog row family:
- `fixtures` → `GET /v1/{sport}/fixtures?date={YYYY-MM-DD}&limit={n}`
- `results` → `GET /v1/{sport}/results?date={YYYY-MM-DD}&limit={n}`
- live (no `live` capability constant exists in the algo's enum today — see
  "What primepredict_algo already has" — so map live to its own short-TTL
  endpoint key when wiring) →
  `GET /v1/{sport}/live` with a very short `cache_ttl_seconds`
- future categories from the roadmap: `status`, `standings`, `odds`,
  `stats`, `form`, `lineups`, `injuries`, richer `h2h`.

Match the algo's `stale_after_seconds` to this service's TTLs (below) so the
algo never reads past the service's own freshness boundary.

**3. Health polling.** The service has no scheduler and no pushes — poll it
on demand. `GET /health` returns per-source status
(`ok|unavailable|blocked|untested`) plus full breaker snapshots
(`state`, `calls`, `consecutive_failures`, `trips`, `cooldown_seconds`,
`open_until_utc`). Map into the algo's health/circuit model:

- Source `blocked`/`unavailable` with `state: open` → the service will 502
  (or skip that source) for cells that depend on that source; feed the algo's
  circuit inputs (`consecutive_failures`, failure-rate history) so its own
  circuit opens instead of hammering a half-cooldown service. `trips` /
  `cooldown_seconds` / `open_until_utc` tell you the service-side retry
  horizon (cooldown doubles 1800 s → 14400 s cap per trip).
- All `untested` at boot means "no request has reached a source yet" — not an
  outage; the first real request both tests the source and, on success, flips
  it to `ok`.
- Per-call `meta.latency_ms` and `meta.cached` on every 200 envelope feed the
  algo's latency and freshness scoring; treat `cached: true` answers as
  freshness-appropriate for the TTL class (30 s live / 1800 s other).

**4. TTL advice** (service cache is per request-key, disk-backed,
success-only; repeat asks within TTL fetch nothing upstream):

- **fixtures/results: poll at a daily cadence.** Fixtures are static-ish; the
  service TTL is 1800 s, which collapses repeat asks into ~1 upstream
  fetch/source/day (SPEC §4). The algo's own `raw_cache` /
  `ProviderEndpoint.cache_ttl_seconds` can sit on top — its existing
  `MetadataOnlyProvider` defaults are fixtures TTL 6 h / stale-after 3 h and
  results TTL 30 d / stale-after 24 h (those are the algo's numbers, not a
  spec).
- **live: poll no faster than every 30 s.** Service TTL is 30 s; anything
  faster just hits the service disk cache and — after expiry — upstream at the
  service's own 1 s/host floor.
- **h2h today: do not poll at all** unless you accept 502s — every shipped
  adapter type-errors h2h (see per-source reality below); schedule it once a
  source gains a verified h2h path.

**5. Error handling.**

| service response | meaning | algo action |
|---|---|---|
| 200 `events: []` (+ optional `meta.warnings`) | **valid empty**: nothing scheduled, or team/entity resolution found nothing | do NOT retry or circuit; record as success; log `meta.warnings` |
| 400 `bad_request` | caller bug (date format/range, `limit` 1..200, h2h missing entity) | fix the request; never retry the same shape |
| 404 `not_found` | unknown sport/category (route typo or pre-roadmap row) | fix the URL/catalog row |
| 502 (code = last source error; body has `detail`, `source`, `tried_sources`) | every source in that row's order failed or was breaker-skipped | back off: the service breaker is likely in cooldown (`/health` confirms; `open_until_utc` is the horizon). Retry at the cooldown boundary, not before — the service never retries into a block by design, and neither should the algo |

The service's terminal error is exactly one per response (the LAST source
error, e.g. `source_unavailable`, `source_blocked`, `rate_limited`,
`not_found`) with `tried_sources` — parse `detail` for probe evidence quotes
before filing bugs against the wrong layer.

**6. Official-first doctrine (SPEC §6, user decision).** For **football**,
primepredict_algo's existing free official **API-Football keys come first** —
the algo already registers `api_football` (plus `football_data_org`,
`thesportsdb`, `football_data_uk`) for that sport. prime-sportdata is the
**fallback/complement**: use it for **tennis, basketball, live** (the service
also carries live tennis/basketball via sofascore), and for anything the
official free tiers lack (football live detail, day-wide fixture views,
per-source warnings). In registry terms: keep the football official providers
at lower (better) priority than `prime_sportdata`; for tennis/basketball
prime-sportdata is the scraper-tier complement after the official
api-sports-family entries where their free tier and flags allow (`api_tennis`,
`api_basketball` exist in the registry; note `key_present` for those is bound
to the same API-Football key settings field).

**7. Phase-2 data needs → future categories.** The algo's Phase-2 list —
odds, form, injury/lineup, calibration, simulation — maps to this service's
roadmap (`docs/architecture.md`): `status/standings/odds/stats/form/
lineups/injuries` are documented future categories and the normalized Event
schema was kept open for them (`score_lines` free labels, strict-but-
extensible statuses, `competition`, `live_detail`, `round_label`). Capability
names already exist on the algo side (`odds, injuries, lineups, stats,
historical`); wiring them = adding catalog rows + per-source verified data
paths on this side (never unverified shapes — SPEC honesty rules).

## Per-source reality today (what to expect per cell)

From `docs/source-health.md` (probe evidence, 2026-09-02) — the /health
statuses make these observable at runtime, not assumptions:

- **flashscore** (football fixtures/results/live default first): day-feed data
  path `2.flashscore.ninja` works over plain httpx with the site's own
  `x-fsign` header. Caveats: dates are served as **Prague-day offsets −1/0/+1**
  (a far `date` 502s that leg); the feed has **no category/live-clock
  semantics** — a "fixtures" answer is the whole Prague day with truthful
  mixed statuses, caller-side filtering, and a standing warning; football rows
  ship **no period score_lines** (the BC/BD pair is not half-time — disproven
  cross-source); unclassifiable tennis end-states are skipped with counted
  warnings. If a future session sees 403s, the `x-fsign` value and resolver
  host must be re-probed from the site's own JS config before anything else.
- **sofascore** (live cells; team-scoped fixtures/results; basketball/tennis
  rows first): JSON API reachable **IPv4-only** — the adapter forces AF_INET
  and tries the session-verified address `151.101.175.52` first, then live
  DNS. The day's DNS answers blackholed from this network at build time; if
  502s resume on sofascore legs, re-probe and update
  `SOFASCORE_IPV4_FALLBACKS` in `sources/sofascore.py` (its docstring says
  so). Needs `team=` for fixtures/results (date-global schedules 404 on this
  API version); tennis fixtures/results and h2h are typed-error cells.
  Sofascore live has true live semantics (`live_detail`, in-play periods).
- **livescore**: blocked at the edge on this network — TCP connect timeouts
  on every 2026-09-02 attempt (SPEC row + this build's re-probe). The adapter
  ships typed `source_unavailable` only, no data path. It sits last in every
  failover order, so it only matters when the first two legs fail; expect it
  in `tried_sources`, rarely as the winner.
- **h2h for all sports** is a typed-error cell today (both sofascore and
  flashscore raise `NotFound` with probe evidence; no team-name→meetings chain
  verified). Live answers with real data were observed for: football fixtures
  (flashscore, 363 events at probe), football live (sofascore, 144),
  basketball/tennis live (sofascore 200s), basketball fixtures/results
  (sofascore team-scoped + flashscore day feeds), tennis day feeds
  (flashscore, 579 rows).

## Non-goals honored

- No edits/installs/git operations inside `/home/flore/primepredict_algo`
  were made by this build (SPEC decision 3) — this guide is the reference the
  algo's own Phase-2 registry work implements.
- No secrets anywhere: the service is keyless localhost; the algo keeps its
  official API keys in its own `.env` (never in docs or code).
