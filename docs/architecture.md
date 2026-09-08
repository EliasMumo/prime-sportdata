# prime-sportdata — architecture

Build date 2026-09-02. This doc describes the shipped v1 system as it actually
exists in this repo. Ground truth: [SPEC.md](../SPEC.md), [source-health.md](source-health.md),
and the code under `src/prime_sportdata/`. Everything about which source
endpoints work or are blocked comes from the live probe evidence in
`docs/source-health.md`; no capability claimed here goes beyond that evidence.

## Scope recap (SPEC v1)

- 3 sports × 4 categories = **12 catalog rows**: `fixtures`, `live`, `results`, `h2h`
  for `football`, `basketball`, `tennis`.
- On-demand only, localhost FastAPI service; no DB, no scheduler, no background
  jobs, no auth.
- Three sources: `flashscore`, `sofascore`, `livescore`. One envelope per request.
- Single-source-wins per request (first success in the configured failover
  order); cross-source entity-id unification is out of scope.

## Component map

```
src/prime_sportdata/
├── config.py          pydantic-settings (env prefix PRIME_SPORTDATA_)
├── models.py          normalized pydantic models + envelope + error body
├── catalog.py         sport × category matrix, params, source-order defaults
├── cache.py           disk cache: data/cache/<sha1>.json.gz (gzip), TTL meta
├── rate_limit.py      per-host min-interval limiter
├── errors.py          typed errors (single class per failure class)
├── sources/
│   ├── base.py        SourceAdapter ABC + SourceResponse + parse helpers
│   ├── sofascore.py   IPv4-forced httpx; endpoints probed 2026-09-02
│   ├── flashscore.py  day-feed data path (2.flashscore.ninja, x-fsign header)
│   └── livescore.py   graceful blocked; typed source_unavailable only
├── engine.py          cache → rate limit → breakers → failover → Envelope
└── server/
    ├── app.py         FastAPI factory (create_app(engine)); uvicorn target `app`
    └── routes.py      /health /catalog /v1/{sport}/{category} + validation
```

### config.py — runtime knobs

pydantic-settings, all overridable via `PRIME_SPORTDATA_*` env vars (e.g.
`PRIME_SPORTDATA_LOG_LEVEL=debug`). Defaults (shipped values):

| setting | default | meaning |
|---|---|---|
| `log_level` | `info` | — |
| `cache_dir` | `data/cache` | disk cache dir; relative to process CWD |
| `rate_limit_interval` | `1.0` s | per-host minimum interval (SPEC ≥ 1 s) |
| `cache_ttl_fixtures_seconds` | `1800` | TTL for fixtures/results/h2h (30 min) |
| `cache_ttl_live_seconds` | `30` | TTL for live (30 s) |
| `breaker_enabled` | `true` | per-source circuit breaker on/off |
| `breaker_cooldown_seconds` | `1800` | first cooldown (30 min) |
| `breaker_cooldown_max_seconds` | `14400` | cooldown cap (4 h) |
| `breaker_trip_after_consecutive` | `2` | 5xx/timeout failures before trip |

Failover-order overrides are caller-supplied mappings into
`catalog.source_order`, keyed `"{sport}.{category}"` (e.g.
`PRIME_SPORTDATA_ORDER_FOOTBALL_FIXTURES` — engine builds overrides from config;
see `catalog.source_order`, which raises `ValueError` on unknown/empty override
source names instead of silently degrading).

### models.py — normalized shapes

Strict pydantic v2 models (`Sport`/`Category`/`EventStatus` are `Literal`s so
unknown values fail loudly). See "Normalized event shape" below. Payload models
(`EventsPayload`, `H2HPayload`) use `extra="forbid"` so the data-union
classification (`EnvelopeData = EventsPayload | H2HPayload`) can never be
misread. Every 200 body is an `Envelope`; every non-200 body is an `ErrorBody`.

### catalog.py — the 12 rows

`ROWS` = 12 `CatalogRow` entries (sport × category). Per-row params:

- fixtures/results: `date, team, league, limit`
- live: `team, league, limit` (live ignores date by construction)
- h2h: `entity_a, entity_b, league, limit`

`limit_default = 50`, `limit_max = 200` (constants on every row).
Default failover orders (SPEC failover table):
`FLASHSCORE_FIRST = (flashscore, sofascore, livescore)` — football
fixtures/results/live; `SOFASCORE_FIRST = (sofascore, flashscore, livescore)` —
basketball/tennis all categories, plus football h2h (sofascore's structured
JSON API first for h2h; the SPEC table lists football categories
exhaustively as fixtures/results/live and leaves h2h to be ordered by the
code's documented rationale). `livescore` sits last everywhere.

### cache.py — DiskCache

Files `data/cache/<sha1(key)>.json.gz` containing
`{"payload": …, "stored_at": <wall clock()>, "ttl_seconds": …}`. Writes are
temp-file + atomic rename; corrupt/truncated entries read as *misses*, never
exceptions. Freshness survives process restarts (wall clock). Engine policy
(live on top, below) decides keys, TTLs and that only successes are cached.

### rate_limit.py — per-host min interval

`acquire(host)` blocks until ≥ `interval` since the previous acquire for the
*same* host; different hosts never block each other; per-host locks serialize
concurrent callers on one host without serializing the process. Engine keys by
adapter host via `engine.DEFAULT_HOSTS`:
`flashscore → 2.flashscore.ninja`, `sofascore → www.sofascore.com`,
`livescore → www.livescore.com` (overridable in the Engine constructor).

### errors.py — typed failure classes

Every adapter failure crosses the boundary as exactly one of these (never a
bare exception). `code` mirrors the ErrorBody code the API may emit.

| class | code | meaning | breaker effect |
|---|---|---|---|
| `SourceUnavailable` | `source_unavailable` | timeout / TCP-DNS failure / 5xx storm | counts toward trip (2 consecutive) |
| `SourceBlocked` | `source_blocked` | 403 / challenge page / block signature | trips immediately (kind=blocked) |
| `RateLimited` | `rate_limited` | HTTP 429 / throttle | trips immediately (kind=blocked) |
| `NotFound` | `not_found` | path not on this source (evidence of shape change) | resets consecutive counter |
| `NoData` | `no_data` | source answered, content yielded nothing usable | resets counter (or wins as resolution-empty from fetch — see engine) |
| `BadRequest` | `bad_request` | query rejected by the source | resets counter |
| `DependencyMissing` | `dependency_missing` | optional extra absent (scrapling) | n/a (adapter-internal) |

`http_status` on an error class is only an upstream hint; terminal HTTP mapping
belongs to the routes (400/404/502 only, see error table below).

### sources/ — the three adapters

Contract (`sources/base.py`): `SourceAdapter.fetch(sport, category, params) ->
SourceResponse` performs the real HTTP request and raises only typed errors;
`parse_events(resp) -> ParseOutcome` parses a real `SourceResponse` into
`Event`s plus honesty `warnings`; `stamp_provenance` anchors every event to the
response it came from (source name + real requested URL); `parse_json_response`
raises `NoData` on undecodable content. Each adapter uses **one static
realistic Chrome UA, no rotation**, max 2 retries with jittered exponential
backoff on timeouts/5xx only (never on 403/429), and per-host spacing
enforced by the engine's rate limiter.

What ships per source today (endpoint evidence in `docs/source-health.md`):

- **flashscore** — data path: `GET https://2.flashscore.ninja/2/x/feed/f_<sportId>_<offset>_3_en_2`
  with header `x-fsign: SW9D1eZo` (sportId football=1/tennis=2/basketball=3;
  offset −1/0/+1 against the server's Europe/Prague date; verified live
  2026-09-02). Pipe format, parsed per sport. No category semantics in the URL:
  the day doc mixes truthful statuses (AB=1 scheduled / AB=2 live / AB=3
  finished or postponed) and parses carry a standing warning that
  fixtures/live/results filtering is caller-side. Football rows ship **no**
  `score_lines` (BC/BD disproven as half-time on probe); basketball finished
  rows ship Q1–Q4 only when all four pairs are ints and sum to the final;
  tennis ships S1.. games. `live_detail` is always None (AC field is not a live
  clock — disproven). Tennis AB=3 no-score rows (AC ∈ {5,9}) are skipped with a
  counted warning. h2h raises typed `NotFound` (df_hh verified per-event-id
  only; no team→event resolution chain). The scrapling escalation hook
  (`research` extra) exists but is inactive by design; absent extra raises
  `DependencyMissing` (lazy import; unit-tested).
- **sofascore** — data paths: `www.sofascore.com/api/v1/sport/{football|basketball|tennis}/events/live`;
  `/team/{id}/events/next/0` and `/last/0` (fixtures/results, needs `team=`
  resolution via `/search/all?q=`); football/basketball only. Client forces
  IPv4 and tries the session-verified address `151.101.175.52` first, then live
  DNS (both AF_INET); IPv6 and the day's DNS answers blackhole on this network.
  Team pages return the source's next/last 30 regardless of date → standing
  warning. Date-only fixtures/results, tennis fixtures/results, and h2h raise
  typed `NotFound` (probe evidence quoted in detail).
- **livescore** — no data path ships. `fetch()` performs the real request to
  the single probed URL (`https://www.livescore.com/en/`) and maps the observed
  outcome: this build observed TCP connect timeouts → `SourceUnavailable`
  quoting probe evidence; future 403/challenge → `SourceBlocked`, 429 →
  `RateLimited`, 5xx → `SourceUnavailable`, an unexpected clean 200 passes
  through for `parse_events`, which raises `NoData` on any input (no content
  contract was ever verified). All 12 cells behave identically.

### engine.py — the fetch pipeline

`Engine.fetch_on_demand(sport, category, params) -> Envelope`. Order is exactly:

1. **Catalog lookup + failover order** — unknown sport/category → typed
   `NotFound`. Order = `catalog.source_order` (default or override).
2. **Cache lookup** — sha1 over canonical JSON of sport + category + sorted
   string-valued params. Fresh entry short-circuits: `meta.cached=true`,
   `latency_ms=0`, adapter never touched. Stale entries are refetched and
   overwritten on success (no stale-if-error). Only successful answers are
   cached; an all-fail request is never cached. TTL: `live` → 30 s; fixtures/
   results/h2h → 1800 s.
3. **Rate limiter** — `acquire(host)` before EVERY adapter call.
4. **Breaker gate** — per source, `allow()` (skip while open).
5. **Ordered failover** — first source that answers wins. Special rule:
   `NoData` raised *from fetch* (e.g. sofascore team search found nothing) is a
   resolution-style empty: that source wins with **200 `events: []`** and the
   error detail as a warning — never a wrong-team guess, never an extra fetch
   into another source. `NoData` from *parse* (healthy response, unusable
   content), NotFound, BadRequest, SourceBlocked, RateLimited,
   SourceUnavailable, and untyped adapter exceptions (wrapped in
   `SourceUnavailable`) are stepping stones: failover continues; each is
   recorded on that source's breaker.
6. **Envelope** — `meta.source` = answering source; `meta.warnings` = collected
   parse warnings; `fetched_at_utc`/`latency_ms` from the injected clock pair;
   events truncated to `limit` when given (the envelope never exceeds it).
   h2h payloads carry the requested entity names (`summary` stays None — no
   adapter derives one this build).
7. **Terminal failure** — every ordered source failed (or was skipped by open
   breakers) → `FetchFailed` with the *last* error's code/detail/source,
   `tried_sources` = the ordered sources, and `skipped_open` mentioned in
   detail when non-empty. Routes map `FetchFailed` → HTTP 502.

Server wiring (`server/app.py`): `create_app(engine=None)`; the module-level
`app` used by uvicorn resolves a real `Engine` lazily on the first request
(`data/cache` is only created then). Tests inject engines over fake adapters
through `app.state.engine_provider`.

## Circuit breaker state machine (per source, in-memory)

States `closed → open → half_open`, all transitions serialized under a per-
breaker lock (concurrent requests cannot double-admit probes):

- **trip to `open`**: one `SourceBlocked` or `RateLimited` (ban signals), or
  `breaker_trip_after_consecutive` (default **2**) *consecutive*
  `SourceUnavailable` (5xx/timeout class). `NotFound`/`NoData`/`BadRequest`
  prove the transport answered → they reset the consecutive counter instead of
  tripping. Healthy responses (incl. valid empties) never trip.
- **while `open`**: the source is skipped in failover (adapter NOT called);
  state surfaced via `/health`.
- **after the cooldown** elapses, the next request reaching the source is a
  **half-open probe** (exactly one; further requests denied until it resolves).
  Probe success → `closed` with escalation level reset. Probe failure (ANY
  typed error) → `open` again, escalated.
- **cooldown escalation**: doubles per trip from `breaker_cooldown_seconds`
  (1800 s = 30 min) toward `breaker_cooldown_max_seconds` (14400 s = 4 h):
  30 min → 1 h → 2 h → 4 h, capped. Recovery resets the level.

`/health` source statuses: `ok | unavailable | blocked | untested`
(untested = never called since process start; blocked = opened by a
403/429-class signal; unavailable = opened by 5xx/timeout class).
Break state is in-memory only — a process restart resets breakers; the disk
cache survives restarts.

Never retry into a block: adapters raise 403/429 before any retry; at engine
level that trips the breaker and failover moves on within the same request.

## HTTP error mapping (routes.py)

| HTTP | body code | when |
|---|---|---|
| 200 | — | success envelope; includes **valid empty** (`data.events: []` — e.g. nothing scheduled, unknown team) and resolution-empty-with-warning |
| 400 | `bad_request` | `date` not strictly YYYY-MM-DD (calendar-validated, e.g. `2026-02-30` out); `limit` not an int or outside 1..200; h2h missing `entity_a`/`entity_b` |
| 404 | `not_found` | unknown sport or category (`/v1/{sport}/{category}` handler); FastAPI's default body for unmatched paths |
| 502 | code of the **last** error (`source_unavailable`/`source_blocked`/`rate_limited`/`not_found`/`no_data`…) | every ordered source failed or was breaker-skipped; `tried_sources` lists the ordered sources; `skipped_open` noted in detail |

Blocked/rate-limited/5xx per source are stepping stones to the 502, never
terminal on their own; there is exactly one terminal error code per response.
Body shape (both cases):
`{"error": {"code": …, "detail": …, "source": …|null, "tried_sources": […]}}`.

## Normalized event shape (v1)

```json
{
  "sport": "football|basketball|tennis",
  "external": {"source": "flashscore", "source_event_id": "...", "source_url": "..."},
  "start_time_utc": "2026-09-02T18:00:00+00:00 | null",
  "status": "scheduled|live|finished|postponed|cancelled|interrupted",
  "home":  {"name": "...", "source_id": null, "score": null|int, "current_score": bool},
  "away":  {"name": "...", "source_id": null, "score": null|int, "current_score": bool},
  "score_lines": [{"period_label": "H1|H2|FT|Q1..Q4|OT|S1..", "home": int|null, "away": int|null}],
  "competition": {"name": "...", "country": "..."|null} | null,
  "live_detail": "73' | Q3 04:12 | 2nd set" | null,
  "round_label": null
}
```

Conventions as implemented: tennis `home`/`away` are players, sets are
`score_lines` (`S1..`), `current_score` marks the in-play set where derivable;
sofascore fills `live_detail` from the source status description for live
events, flashscore always None (AC disproven as a clock); `source_id` only from
sofascore (flashscore day feed carries no numeric ids); `score_lines` football:
sofascore H1/H2/FT when present in the payload, flashscore none (see above);
basketball: Q1..Q4 (+guarded OT for sofascore). Scores/`current_score` only
meaningful while live.

## Coverage matrix — the 12 cells at build time (2026-09-02)

Which source serves which cell *today*, from the probe evidence in
`docs/source-health.md` (probe rows) plus the engine's default orders:

| sport | fixtures | results | live |
|---|---|---|---|
| **football** | **flashscore** day feed (offset per `date`; only −1/0/+1 vs Prague date verified; whole-day doc, caller-side filtering; mixed truthful statuses — "fixtures" means "the day's matches") | **flashscore** day feed (adapter's no-date fallback = Prague yesterday; past-day docs contain only finished/postponed rows — 226 events at probe; the route default date is today UTC, so an effective offset of 0 or −1 depending on time of day) | **flashscore** day doc first in order (truthful statuses incl. live rows; caller-side live filter); **sofascore** `/sport/football/events/live` is the semantically-true live fallback (144 events parsed at probe) |
| **basketball** | **sofascore** team-scoped next-page when `team=` given (search resolves; else 200-empty + warning); date-only → **flashscore** day feed (10 events at probe) | **sofascore** team-scoped last-page when `team=`; date-only → **flashscore** day feed | **sofascore** `/sport/basketball/events/live` (200; empty at probe — valid empty is a real answer) |
| **tennis** | **flashscore** day feed (579 rows at probe); sofascore raises typed `NotFound` (no verified player event list) | **flashscore** day feed | **sofascore** `/sport/tennis/events/live` (92 events at probe); flashscore day doc also carries live rows |

**h2h (all three sports)**: typed-error cell today. sofascore raises
`NotFound` (meeting lists 404 tokenless; the 200 aggregate-duel endpoint has no
events/side names), flashscore raises `NotFound` (df_hh verified but needs a
source event id; no team→event resolution), livescore unavailable. An h2h
request therefore fails over all three sources → HTTP 502 (engine
`FetchFailed`; body carries the last error's code, e.g. `not_found`, and
`tried_sources: [sofascore, flashscore, livescore]`) for football — unless an
upstream proves reachable in a future session. The
200 h2h envelope shape exists in `models.py` and is route-level tested with a
fake answering adapter, but no real adapter produces it this build.

Live end-to-end confirmation (docs stage, 2026-09-02): `GET /v1/football/fixtures?limit=3`
against the running server returned HTTP 200 from `flashscore` (3 parsed
scheduled events, standing warning present). See `docs/source-health.md`
consolidation table and `docs/api-contract.md` for the captured output.

## Roadmap, extension points, reverse decisions

**Roadmap categories** (SPEC: documented here, schema must not close the door):
`status`, `standings`, `odds`, `stats`, `form`, `lineups`, `injuries` —
mirroring the algo's phase-2 data needs (see `docs/integration-guide.md`).
None ship today; the normalized `Event` already carries the fields these would
build on (`status`, `score_lines`, `competition`, `live_detail`, `round_label`).

**Adding a source**: subclass `SourceAdapter` in `sources/<name>.py`; register
the name in `catalog.SOURCES` and the per-cell orders; add it to the adapters
dict in `server/app._build_default_engine` and to `engine.DEFAULT_HOSTS`;
record fixture files under `tests/fixtures/<source>/…`; probe live and record
rows in `docs/source-health.md` before any URL ships (SPEC: never ship a code
path that returns data from an endpoint not actually hit and parsed this
build). Fetch backends sit behind `sources/base.py`, which is what makes
tooling decisions reversible.

**Adding a category**: extend the `Category` literal in `models.py`, add the
catalog row (+ failover order), implement per-source fetch/parse or typed
errors, extend `_data_payload` for new envelope payloads. The server route is
generic (`/v1/{sport}/{category}`) and needs no change.

**Schema openness notes**: `ScoreLine.period_label` stays a free string so odd
source labels can be carried honestly; `EventStatus` is a strict Literal — 
unknown source statuses are skipped with a counted warning, never guessed;
payload models `extra="forbid"` keep the data-union classification
unambiguous; unknown source status codes inside adapters surface as
`warnings[]`, never silently.

**Reverse decisions** (recorded so they are not re-litigated blindly):
- *crawl4ai not adopted* (SPEC §7): Playwright-core-based, heavier and more
  fingerprintable than curl_cffi impersonation; extraction features aimed at
  content pages, not structured feeds. Reversible because fetch backends sit
  behind `sources/base.py`.
- *No Firecrawl, no cloud, no paid APIs* (SPEC §2): matches the algo's
  free-tier constraint.
- *Scrapling `research` extra*: present but inactive — the plain-httpx
  flashscore feed path was verified live and is the shipped mechanism; the
  escalation hook raises `DependencyMissing` when the extra is absent.
- *Sofascore date-global schedules and meeting lists*: this API version 404s
  them without a session token; team-scoped pages or typed errors instead.
- *Flashscore live clock*: not exposed; `AC` empirically disproven — no
  `live_detail` for flashscore events, live filtering caller-side.
- *Livescore*: blocked at the edge on this network (typed `source_unavailable`
  only); revisit only after a future probe records a real HTTP response.

## Quality gates (reproduce)

```bash
cd /home/flore/primesportdata
.venv/bin/ruff check --no-cache .
.venv/bin/mypy --cache-dir /tmp/pss_mypy_cache src
.venv/bin/python -m pytest -q            # offline unit suite (no network)
.venv/bin/python -m pytest -q -m live    # opt-in real-network smoke
.venv/bin/uvicorn prime_sportdata.server.app:app --port 8097
```
