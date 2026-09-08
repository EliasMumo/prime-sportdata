# prime-sportdata — API contract

Base URL: `http://127.0.0.1:8097` (SPEC uvicorn command — run from the repo
root so the `data/cache` default resolves under the repo):

```bash
cd /home/flore/primesportdata
.venv/bin/uvicorn prime_sportdata.server.app:app --port 8097
```

All routes return JSON. Every 200 body is an `Envelope`; every non-200 body is
an `ErrorBody` (shapes below). This contract was live-verified on 2026-09-02
against the running server: outputs in this doc marked *captured* are verbatim
curl output from that session. Error-path curls are local-only; exactly one
`/v1` data curl (an external fetch) was made, per the SPEC politeness budget.

## Endpoints (route table)

| method/path | purpose |
|---|---|
| `GET /health` | liveness + per-source status and circuit-breaker snapshot |
| `GET /catalog` | the 12 sport×category rows: params, source failover order, example curl |
| `GET /v1/{sport}/fixtures` | upcoming/day matches (`date` default today) |
| `GET /v1/{sport}/results` | finished matches (`date` default today UTC at the route; adapter-side fallback yesterday — results need the requested day's rows) |
| `GET /v1/{sport}/live` | matches in play right now (`date` ignored by construction) |
| `GET /v1/{sport}/h2h` | head-to-head: `entity_a`+`entity_b` required |

`sport ∈ {football, basketball, tennis}`; `category ∈ {fixtures, live,
results, h2h}`. Anything else → 404 `not_found` error body. Params come from
the URL query string. `team`/`entity_a|b` are *advisory*: the source resolves
the name; failed resolution is 200 `events: []` + warning, never a
wrong-team guess (SPEC).

## GET /health

Response body:

```json
{
  "status": "ok",
  "sources": {"flashscore": "ok|unavailable|blocked|untested",
              "sofascore": "…", "livescore": "…"},
  "breakers": { "<source>": {"status": "…", "state": "closed|open|half_open",
              "calls": 0, "consecutive_failures": 0, "trips": 0,
              "cooldown_seconds": 1800.0, "open_until_utc": null} }
}
```

- `sources` values (SPEC): `ok` (calls happened, breaker closed) |
  `unavailable` (opened by 5xx/timeout class) | `blocked` (opened by
  403/429/challenge class) | `untested` (no call reached this source yet this
  process run).
- `breakers.<source>.state` is the state machine snapshot; `open_until_utc`
  is set while open/half_open; `trips` counts trips (drives cooldown
  escalation: 1800 s doubling to 14400 s cap); `consecutive_failures` counts
  5xx/timeout-class failures toward the trip threshold of 2.

Captured (server freshly started, no data calls yet) — HTTP 200:

```bash
curl -s http://127.0.0.1:8097/health
```

```json
{"status":"ok","sources":{"flashscore":"untested","sofascore":"untested","livescore":"untested"},"breakers":{"flashscore":{"status":"untested","state":"closed","calls":0,"consecutive_failures":0,"trips":0,"cooldown_seconds":1800.0,"open_until_utc":null},"sofascore":{"status":"untested","state":"closed","calls":0,"consecutive_failures":0,"trips":0,"cooldown_seconds":1800.0,"open_until_utc":null},"livescore":{"status":"untested","state":"closed","calls":0,"consecutive_failures":0,"trips":0,"cooldown_seconds":1800.0,"open_until_utc":null}}}
```

After one successful `/v1/football/fixtures` call the same endpoint reports
`"flashscore": "ok"` with `calls: 1` (captured — the answering source moves
from `untested` to `ok`).

## GET /catalog

Captured (abridged to the row fields) — HTTP 200, 12 rows:

```bash
curl -s http://127.0.0.1:8097/catalog
```

| # | sport | category | params | sources (failover order) | example_curl (from the row) |
|---|---|---|---|---|---|
| 1 | football | fixtures | date, team, league, limit | flashscore, sofascore, livescore | `curl -s 'http://127.0.0.1:8097/v1/football/fixtures?date=2026-09-02&team=Arsenal&league=Premier+League&limit=50'` |
| 2 | football | live | team, league, limit | flashscore, sofascore, livescore | `curl -s 'http://127.0.0.1:8097/v1/football/live?team=Arsenal&league=Premier+League&limit=50'` |
| 3 | football | results | date, team, league, limit | flashscore, sofascore, livescore | `curl -s 'http://127.0.0.1:8097/v1/football/results?date=2026-09-02&team=Arsenal&league=Premier+League&limit=50'` |
| 4 | football | h2h | entity_a, entity_b, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/football/h2h?entity_a=Team+A&entity_b=Team+B&league=Premier+League&limit=50'` |
| 5 | basketball | fixtures | date, team, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/basketball/fixtures?date=2026-09-02&team=Arsenal&league=Premier+League&limit=50'` |
| 6 | basketball | live | team, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/basketball/live?team=Arsenal&league=Premier+League&limit=50'` |
| 7 | basketball | results | date, team, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/basketball/results?date=2026-09-02&team=Arsenal&league=Premier+League&limit=50'` |
| 8 | basketball | h2h | entity_a, entity_b, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/basketball/h2h?entity_a=Team+A&entity_b=Team+B&league=Premier+League&limit=50'` |
| 9 | tennis | fixtures | date, team, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/tennis/fixtures?date=2026-09-02&team=Arsenal&league=Premier+League&limit=50'` |
| 10 | tennis | live | team, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/tennis/live?team=Arsenal&league=Premier+League&limit=50'` |
| 11 | tennis | results | date, team, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/tennis/results?date=2026-09-02&team=Arsenal&league=Premier+League&limit=50'` |
| 12 | tennis | h2h | entity_a, entity_b, league, limit | sofascore, flashscore, livescore | `curl -s 'http://127.0.0.1:8097/v1/tennis/h2h?entity_a=Team+A&entity_b=Team+B&league=Premier+League&limit=50'` |

Every row also carries `"limit_default": 50, "limit_max": 200`. Note the
failover order difference: football h2h and all basketball/tennis rows start
with `sofascore`; football fixtures/live/results start with `flashscore`
(SPEC failover table + catalog rationale). The example curls above are the
exact strings the API emits (verbatim from the captured /catalog response,
rows 1–12).

## GET /v1/{sport}/fixtures | /results | /live | /h2h

### Query parameters

| param | applies to | default | validation |
|---|---|---|---|
| `date` | fixtures, results | today (UTC date, set by the route) | strictly `YYYY-MM-DD` and a real calendar date (`2026-02-30`, `2026-9-2`, `2026-13-01` → 400). NOTE: flashscore serves only Prague-day offsets −1/0/+1 — a date farther out fails that source (`not_found` in the 502 chain); the flashscore adapter's own no-date fallback is today for fixtures and Prague-yesterday for results |
| `team` | fixtures, results, live | none (optional) | free text; advisory — resolved per source (sofascore search; flashscore day doc ignores it with a standing warning) |
| `entity_a`, `entity_b` | h2h | none | **both required**; blank/absent → 400 |
| `league` | all | none (optional) | free text, advisory, no server validation |
| `limit` | all | 50 | integer 1..200; non-int or out of range → 400 |

### Success envelope (200)

`data` is `{"events": [...]}` for fixtures/results/live and
`{"events": [...], "home": {"name","summary"}, "away": {"name","summary"}}`
for h2h (`summary` null unless a source derives one). Captured verbatim
(`limit=3` truncation; one of three events shown, response was HTTP 200):

```bash
curl -s 'http://127.0.0.1:8097/v1/football/fixtures?limit=3'
```

```json
{"data":{"events":[{"sport":"football","external":{"source":"flashscore","source_event_id":"pdmRhdwH","source_url":"https://2.flashscore.ninja/2/x/feed/f_1_0_3_en_2"},"start_time_utc":"2026-09-02T18:00:00+00:00","status":"scheduled","home":{"name":"Atl. Tucuman 2","source_id":null,"score":null,"current_score":false},"away":{"name":"San Lorenzo 2","source_id":null,"score":null,"current_score":false},"score_lines":[],"competition":{"name":"Reserve League - Clausura","country":"Argentina"},"live_detail":null,"round_label":null}],"meta":{"sport":"football","category":"fixtures","source":"flashscore","cached":false,"fetched_at_utc":"2026-09-02T16:36:05.590836+00:00","latency_ms":1521,"request":{"date":"2026-09-02","league":null,"limit":3},"warnings":["day-table feed returns the full source day with truthful statuses and carries no live clock/period detail (the AC field is not the live minute — disproven on probe) — fixtures/live/results filtering and live_detail are caller-side (source=flashscore, url=https://2.flashscore.ninja/2/x/feed/f_1_0_3_en_2)"]}}}
```

`meta.request` echoes the query; `meta.warnings` carries honesty notes
(standing parse caveats, skipped events) — the example shows the flashscore
caller-side-filtering warning. An unknown team is a valid-empty 200: adapter
resolution misses become `"events": []` with the miss reason in
`meta.warnings`, never an error and never a wrong-team row.

### Error bodies (non-200)

Uniform shape: `{"error": {"code": ..., "detail": ..., "source": ...|null, "tried_sources": [...]}}`.

Captured verbatim (all four are local-only, no external fetch):

```bash
curl -s 'http://127.0.0.1:8097/v1/cricket/fixtures'        # unknown sport -> 404
curl -s 'http://127.0.0.1:8097/v1/football/live?limit=500'  # limit out of range -> 400
curl -s 'http://127.0.0.1:8097/v1/football/h2h'             # h2h missing entities -> 400
curl -s 'http://127.0.0.1:8097/v1/football/fixtures?date=2026-13-99'  # bad date -> 400
```

HTTP 404:

```json
{"error":{"code":"not_found","detail":"unknown sport or category: /v1/cricket/fixtures (known sports: football, basketball, tennis; categories: fixtures, live, results, h2h)","source":null,"tried_sources":[]}}
```

HTTP 400 (limit):

```json
{"error":{"code":"bad_request","detail":"limit must be between 1 and 200, got '500'","source":null,"tried_sources":[]}}
```

HTTP 400 (h2h entities):

```json
{"error":{"code":"bad_request","detail":"h2h requires both entities: entity_a, entity_b missing","source":null,"tried_sources":[]}}
```

HTTP 400 (date):

```json
{"error":{"code":"bad_request","detail":"date must be YYYY-MM-DD, got '2026-13-99'","source":null,"tried_sources":[]}}
```

HTTP 502 (all sources failed): returned when every source in the row's
failover order failed or was skipped by an open breaker. Body carries the
**last** error's code (`source_unavailable`, `source_blocked`,
`rate_limited`, `not_found`, …), its detail/source, `tried_sources` (the
ordered sources), and — when sources were skipped — that note inside
`detail`. Example shape (not live-curled in the docs pass; every h2h curl
would drive a real request into the blocked livescore host, so the 502 path
was verified offline instead — unit tests with fake all-down adapters assert
exactly this body and code: `tests/test_server.py::test_v1_all_sources_down_is_502_with_last_code_and_tried_sources`):

```json
{"error":{"code":"<code of the last source error>","detail":"…","source":"<last source>","tried_sources":["sofascore","flashscore","livescore"]}}
```

Today, real `h2h` requests for all three sports end in this 502 path: every
shipped adapter type-errors h2h (evidence in `docs/source-health.md`), so the
engine's `FetchFailed` terminal outcome is the honest answer until a source
gains a verified h2h data path. Note the 200 h2h envelope shape exists and is
route-tested with a fake answering adapter, but no real adapter produces it
this build.

### Status code summary

| status | meaning |
|---|---|
| 200 | success — including valid empty `events: []` (nothing scheduled / team not found) |
| 400 `bad_request` | malformed/out-of-range param (date, limit) or h2h without both entities — caller bug |
| 404 `not_found` | unknown sport/category (also FastAPI's default body on unmatched paths) |
| 502 (last-error code) | every source in the failover order failed/was breaker-skipped — service or upstream problem; inspect `detail` + `tried_sources`, retry later (breakers re-probe after cooldown) |

## Politeness notes for consumers

- Caching is server-side per request key: `live` answers are cached 30 s;
  fixtures/results/h2h 1800 s. Repeat asks within TTL are served from disk
  (`meta.cached: true`, `latency_ms: 0`) with no upstream fetch — the model's
  daily fixture cadence collapses to ~1 upstream fetch/source/day.
- The service rate-limits itself to ≥ 1 s per upstream host; consumers need no
  extra throttling, but should not fire tens of `/v1` requests concurrently —
  they serialize on the upstream limiter.
- Exactly one `/v1` data example was live-fired in this docs session
  (above). Consumers should treat `/v1/football/fixtures` (live cell) as
  representative, not a permission to sweep.
