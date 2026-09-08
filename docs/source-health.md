# source-health — live probe evidence (2026-09-02)

Probe session of the sofascore API for the Stage 3 adapter build. Machine:
Kali (this network), one static realistic Chrome UA, sequential probes >= 1 s
apart, connect/read timeout ~10-12 s, max 2 retries on timeouts/5xx only,
session aborted after 2 consecutive 5xx/timeouts (SPEC Ban-risk policy).
Nothing below is inferred: every row is a request made during this build's
probe session, with its observed status and response shape.

## Session log

1. **First httpx session (13:50 UTC-ish)** — the shipped-client mechanism
   (httpx + AF_INET-only getaddrinfo shim, no address pinning) hit
   `www.sofascore.com/api/v1/sport/football/scheduled-events/2026-09-02` and
   `.../events/live`: **2 consecutive connect timeouts → session aborted per
   the 2-consecutive-failure rule**. curl re-verification then showed TCP
   connect timeouts on both hosts, both ports (80/443), and **all four A
   records in DNS at that moment** (151.101.3.52 / .67.52 / .131.52 / .195.52,
   shared by www + api), while other sites (example.com, reddit.com, a Fastly
   host) answered normally. IPv6 also timed out.
2. **Route discovery** — the SPEC evidence row recorded A=151.101.175.52
   answering earlier that day. A single `curl -4 --resolve
   www.sofascore.com:443:151.101.175.52` connected instantly (HTTP 404 JSON,
   API origin alive). That address serves **both** `www.sofascore.com` and
   `api.sofascore.com` (SNI-routed Fastly edge). Conclusion recorded in the
   adapter: DNS answers of the day blackhole from this network while the
   session-verified address answers → adapter tries the verified address
   first, then live DNS results (both AF_INET-only).
3. **Path-discovery session** — the probe table below (all via the verified
   route; ~25 HTTP requests total this session, then probing stopped).

## Probe table (2026-09-02)

| source | host/path(s) tried | live status | response shape snippet | verdict | fixture(s) recorded |
|---|---|---|---|---|---|
| sofascore | `www.sofascore.com/api/v1/sport/football/events/live` | 200 | `{"events":[{"eventState":{},"tournament":{"name":"Coppa Italia",...` (85 events) | **works** | tests/fixtures/sofascore/football/live.json |
| sofascore | `www.sofascore.com/api/v1/sport/basketball/events/live` | 200 | `{"events":[]}` (13 B, valid empty) | **works** | basketball/live_empty.json |
| sofascore | `www.sofascore.com/api/v1/sport/tennis/events/live` | 200 | `{"events":[{"firstToServe":1,"tournament":{"name":"Porto, Portugal",...` (92 events) | **works** | tennis/live.json |
| sofascore | `www.sofascore.com/api/v1/search/all?q=barcelona` | 200 | `{"results":[{"entity":{"id":2817,"name":"FC Barcelona",...` (team + player entities) | **works** (team resolution: entity.type==0 + sport slug) | search/search_all_barcelona.json |
| sofascore | `www.sofascore.com/api/v1/team/2817/events/next/0` (FC Barcelona football) | 200 | `{"events":[{... "tournament":{"name":"LaLiga"...`, 30 upcoming | **works** (fixtures, team-scoped) | football/team_next.json |
| sofascore | `www.sofascore.com/api/v1/team/2817/events/last/0` | 200 | `{"events":[{...`, 30 recent (finished) | **works** (results, team-scoped) | football/team_last.json |
| sofascore | `www.sofascore.com/api/v1/team/3540/events/next/0` and `.../last/0` (Real Madrid basketball) | 200 | `{"events":[...],"hasNextPage":true}`; finished event scores `homeScore":{"current":87,"period1":25,...period4:19}` | **works** (bb periods Q1-Q4 verified) | basketball/team_next.json + team_last.json |
| sofascore | `www.sofascore.com/api/v1/sport/{football,basketball,tennis}/scheduled-events/2026-09-02` (both hosts) | 404 | `{"error":{"code":404,"message":"Not Found"}}` (44 B) | **wrong-path** (date-global schedule not on this API version) | errors/not_found_scheduled.json |
| sofascore | `www.sofascore.com/api/v1/sport/football/events/2026-09-02` + finished-events variants (fb/bb) | 404 | same 44 B JSON | **wrong-path** | — |
| sofascore | `api.sofascore.com/api/v1/sport/football/scheduled-events/2026-09-02` + `events/2026-09-02` | 404 | same 44 B JSON | **wrong-path** | — |
| sofascore | `www.sofascore.com/api/v1/event/{id}/h2h/events` (both hosts) | 404 | same 44 B JSON | **wrong-path** (meeting lists token-gated) | — |
| sofascore | `api.sofascore.com/api/v1/event/16860534/h2h` | 200 | `{"teamDuel":{"homeWins":6,"awayWins":2,"draws":2},"managerDuel":{...}}` (102 B) | **works but unusable for the h2h envelope** (aggregate counts only — no events, no side names) | h2h/event_duel_aggregate.json |
| sofascore | `www.sofascore.com/api/v1/team/462177/events/next/0` (tennis player) | 404 | same 44 B JSON | **wrong-path** (tennis players not under /team/) | tennis/team_player_404.json |
| sofascore | `www.sofascore.com/api/v1/event/16860534` | 200 | `{"event":{"eventState":{},"tournament":{... "homeScore":{...}}}` | works (detail shape recorded; not needed by shipped paths) | — (kept in probe scratch) |
| sofascore | both hosts, all DNS A records of the day (151.101.3/67/131/195.52), ports 80+443, v4+v6 | TCP timeout | — | **unreachable route today** (see session log; 151.101.175.52 answers instead) | — |

## What ships as data paths vs typed errors (Stage 3 adapter)

Verified + shipped (parsed from real recorded responses in offline tests):

- `GET www.sofascore.com/api/v1/sport/{football|basketball|tennis}/events/live` → live events.
- `GET www.sofascore.com/api/v1/team/{id}/events/next/0` → fixtures (needs `team=`; source page = next 30, date-agnostic — parse carries a standing warning).
- `GET www.sofascore.com/api/v1/team/{id}/events/last/0` → results (same caveat).
- `GET www.sofascore.com/api/v1/search/all?q=` → team/entity resolution for the above.

Typed-error paths with evidence (raised as `NotFound`/`NoData`, detail quotes this
table): date-only fixtures/results for all sports (date-global endpoints 404),
tennis fixtures/results (no verified player event list), h2h for all sports
(meeting lists 404 tokenless; the 200 aggregate-duel endpoint cannot name the
sides and carries no events, so the SPEC h2h envelope cannot be built honestly).
403/429/5xx/timeout map to SourceBlocked/RateLimited/SourceUnavailable; the
client forces IPv4 and tries the session-verified address 151.101.175.52
first. Failover (football fixtures/results/live → flashscore first) keeps the
covered cells served by other sources (catalog defaults, SPEC failover table).

## `pytest -q -m live` run (adapter real-network path)

Run on 2026-09-02, after the probe session above, using the shipped adapter
(httpx + AF_INET shim with the verified address tried first, one static UA,
>= 1.3 s spacing): **4 passed, 76 deselected** (exit 0). Real results through
the adapter's own network path:

- `fetch("football", "live", {})` → HTTP 200, **144 live events parsed** to
  typed Events (provenance stamped, source_url = the real requested URL).
- `fetch("basketball", "fixtures", {"team": "Real Madrid"})` → HTTP 200,
  search resolved basketball Real Madrid (id 3540), **30 upcoming events
  parsed**, team-scoped warning present.
- `fetch("tennis", "live", {})` → HTTP 200, **91 live events parsed**.
- Date-only fixtures and h2h raised typed `NotFound` with evidence (no
  network touched), as designed.

Verdict: sofascore works over IPv4 through the session-verified route for
live (3 sports) and team-scoped fixtures/results (football + basketball);
everything else is a typed-error path with evidence above. Note: DNS answers
of the day blackhole from this network — if 151.101.175.52 stops answering in
a later session, re-probe and update `SOFASCORE_IPV4_FALLBACKS` in
`src/prime_sportdata/sources/sofascore.py`.

## Flashscore (Stage 4, 2026-09-02)

Same session policy as sofascore: one static Chrome UA, sequential probes >= 1
s apart, ~12 s timeouts, max 2 retries on timeouts/5xx only. Hosts touched:
`www.flashscore.com` (1 hit), `static.flashscore.com` (8 hits, JS chunk
archaeology), `2.flashscore.ninja` (9 deliberate hits incl. the final
verification runs of the shipped adapter). Zero 403/429/5xx/timeouts; no
abort needed. One accidental malformed URL (empty event id) returned a 1-byte
body "0" — excluded from evidence below.

### Probe table (2026-09-02)

| source | host/path(s) tried | live status | response shape snippet | verdict | fixture(s) recorded |
|---|---|---|---|---|---|
| flashscore | `www.flashscore.com/football/` (browser UA) | 200 | ~737 KB app shell: no `__NEXT_DATA__`, no embedded JSON, 12 `event` hits in CSS/JS only | app-shell HTML; rows NOT server-rendered -> data path had to be discovered | — (scratch only) |
| flashscore | `static.flashscore.com` webpack chunks (core2.js 2.3 MB etc.) | 200 (x8) | inline `cjs._config`: `feed_sign:"SW9D1eZo"`, `project.id:2`, `feed_resolver.local:[{url:"https://2.flashscore.ninja",...}]`, `default_url:"https://global.flashscore.ninja"`, `sport_list` soccer=1/tennis=2/basketball=3 | JS archaeology -> endpoint family `https://2.flashscore.ninja/2/x/feed/<feed>` + header `x-fsign` | — (scratch only) |
| flashscore | `2.flashscore.ninja/2/x/feed/sys_1` | 200 | `SA÷1¬…¬utime÷1788362251¬~` (system ping, pipe format: field `÷`, pair `¬`, row `~`) | feed protocol works over plain httpx | — |
| flashscore | `.../f_1_0_3_en_2` (football today) | 200 | 620 KB, 363 rows / 97 league blocks; `~ZA÷ANGOLA: Girabola¬ZEE÷…¬ZY÷Angola¬…~AA÷<id>¬AD÷<unix>¬AB÷1|2|3¬AE÷<home>¬AF÷<away>¬AG÷<h score>¬AH÷<a score>¬…`; AB=1 scheduled (209) / 2 live (56) / 3 finished+postponed (98; 3 postponed AC=4) | **works** | football/day_today.txt (trim: 4 whole league sections, one row per status incl. Sassuolo `KxrZoe94`) |
| flashscore | `.../f_1_-1_3_en_2` (football yesterday) | 200 | 135 KB, 226 rows, all AB=3 (6 postponed AC=4) | **works** (results) | football/day_yesterday.txt (trim, 2 sections/5 rows) |
| flashscore | `.../f_1_1_3_en_2` (football tomorrow) | 200 | 351 KB, 184 rows, ALL AB=1 scheduled (85 leagues), earliest kickoff = Prague midnight | **works** (fixtures) | football/day_tomorrow.txt (trim, 2 sections/3 rows) |
| flashscore | `.../f_3_0_3_en_2` (basketball today) | 200 | 6.6 KB, 10 rows / 3 leagues; finished rows carry Q1-Q4 pairs `BA:BB..BG:BH` — sums equal AG/AH on all 7 (21+22+27+6=76 etc.) | **works** | basketball/day_today.txt (whole body verbatim) |
| flashscore | `.../f_2_0_3_en_2` (tennis today) | 200 | 867 KB, 579 rows; `AG`/`AH` = sets won (live rows with completed sets verify: S1 6:4 done -> AG=1), set games `BA:BB..BI:BJ` S1..S5; 28 live rows include the in-play set's pair (S1 4:1 in progress observed) | **works** | tennis/day_today.txt (trim, 9 rows of the US Open section) |
| flashscore | `.../df_hh_1_KxrZoe94` (per-event h2h) | 200 | 54 KB: `~KB÷Last matches: Sassuolo¬KC÷1788354000¬KP÷KxrZoe94¬KF÷Coppa Italia¬KJ÷Sassuolo¬KK÷Frosinone¬KL÷2:1¬KM÷(1:1)¬RPA÷4¬RPB÷3…` | works per-event-id only — NOT shipped as a data path (needs a source event id; no verified team-name -> event resolution this session) | h2h/df_hh_1_KxrZoe94.txt (head slice, evidence for the typed-error cell) |

Semantics resolved empirically (cross-source where possible): Sassuolo-Frosinone
(Coppa Italia, kickoff 2026-09-02T13:00:00Z) appears in the sofascore live
record with H1 1:1 and in the flashscore day feed as finished 2-1 with
`BC:BD = 0:0` -> **football period pairs (BC/BD) are NOT half-time; football
rows ship no score_lines**. The `AC` field is not a live clock (values 7-38
inconsistent with elapsed time, identical values across matches at different
elapsed times) -> `live_detail` is None for all flashscore events; tennis
live rows still ship sets-won scores and the in-play set's S-line. 10 tennis
AB=3 rows without score (AC in {5,9}, one with a "withdrawn" note) are
unclassifiable to SPEC statuses -> skipped with a counted warning. Date
offsets beyond {-1, 0, 1} and per-event h2h without an event id raise typed
NotFound with this table quoted as evidence.

### What ships as data paths vs typed errors (Stage 4 adapter)

Verified + shipped (parsed from real recorded responses in offline tests):

- `GET https://2.flashscore.ninja/2/x/feed/f_<sportId>_<dayOffset>_3_en_2`
  (sportId 1/2/3 = football/tennis/basketball; dayOffset -1/0/+1 vs the
  server's Prague date) -> the day's events for fixtures/results/live.
  Category semantics are NOT in the URL: parse returns truthful statuses and
  carries a standing warning that fixtures/live/results filtering is
  caller-side (same precedent as the sofascore team-scoped warning).

Typed-error paths with evidence: date offsets outside {-1,0,+1} (`NotFound`),
`h2h` for all sports (`NotFound` — df_hh verified but requires an event id;
no team->event resolution chain probed), malformed dates (`BadRequest`),
403/429/5xx/timeouts (`SourceBlocked`/`RateLimited`/`SourceUnavailable`).
The scrapling escalation hook (`research` extra) exists but is inactive by
design — the plain-httpx feed path is verified and shipped; absence of the
extra raises `DependencyMissing` (unit-tested), never ImportError at import.

### `pytest -q -m live` run (adapter real-network path)

Run 2026-09-02 evening through the shipped adapter itself: **8 passed, 100
deselected** (4 sofascore + 4 flashscore live tests, exit 0). Real results:

- `fetch("football","fixtures",{date: today})` -> HTTP 200, **363 events
  parsed** (whole Prague-day doc, truthful mixed statuses).
- `fetch("football","results",{date: yesterday})` -> HTTP 200, **226 events
  parsed** (all finished/postponed).
- `fetch("basketball","fixtures",{date: today})` -> HTTP 200, **10 events
  parsed** (7 finished with Q1-Q4 lines, 2 postponed, 1 scheduled).
- h2h / out-of-range offsets / malformed dates raised typed errors with no
  network touched, as designed.

Verdict: flashscore's day-feed data path works from this network over plain
httpx with the `x-fsign` header; football fixtures (the SPEC primary cell)
are covered for yesterday/today/tomorrow. Everything else is a typed-error
path with evidence above or caller-side filtering with a standing warning.
Note: the `x-fsign` value and ninja resolver host come from the site's own JS
config (core2.js, 2026-09-02) — if a later session sees 403s, re-probe the
config before touching anything else.

## Livescore (Stage 4b, 2026-09-02)

Same session policy as the other sources: one static Chrome UA, sequential
probes >= 1 s apart, ~8 s connect timeout, session aborted after 2
consecutive timeouts/5xx (SPEC Ban-risk policy). Host touched:
`www.livescore.com` only — the first two attempts both TCP-timed-out and the
abort rule stopped the probe there. This was this build's own re-probe of the
SPEC network evidence row, which recorded the same behavior earlier the same
day.

### Probe table (2026-09-02)

| source | host/path(s) tried | live status | response shape snippet | verdict | fixture(s) recorded |
|---|---|---|---|---|---|
| livescore | `https://www.livescore.com/en/` (plain curl, static Chrome UA, connect timeout 8 s, 15:46:20Z) | TCP connect timeout | none — `curl: (28) Connection timed out after 8000 milliseconds`, http=000, no remote_ip, time_total=8.000251 s | **blocked** — edge TCP-times-out this client; matches the SPEC network row | — (no response ever received) |
| livescore | same URL, `curl -4` (forced IPv4), 15:46:30Z | TCP connect timeout | none — `curl: (28) Connection timed out after 8002 milliseconds`, http=000, no remote_ip, time_total=8.002280 s | **blocked** — IPv4 pinning changes nothing | — |

Session aborted after those 2 consecutive connect timeouts. With zero HTTP
responses ever recorded for this host (SPEC evidence + this session) there is
no response shape to parse from, so no data-path discovery was attempted.

### What ships as data paths vs typed errors (Stage 4b adapter)

No data path ships. `src/prime_sportdata/sources/livescore.py` is structured
fully per the base contract (SourceAdapter subclass with fetch() +
parse_events() implemented) but exists to fail gracefully and honestly:

- `fetch()` performs the real request against the single URL ever probed (the
  homepage — not a data endpoint) and maps the observed outcome: transport
  timeouts/refusals raise typed `SourceUnavailable` whose detail quotes the
  2026-09-02 probe evidence above (this build's exact rows). A future session
  whose network behaves differently is mapped, never guessed: HTTP 403 and
  challenge-signature pages -> `SourceBlocked`, 429 -> `RateLimited`, 5xx
  after retries -> `SourceUnavailable`, and an unexpected non-challenge HTTP
  200 passes through raw only for `parse_events` to reject.
- `parse_events()` raises typed `NoData` on any input: no livescore content
  shape was ever verified, so returning events would be an invented contract.
  No fixtures were recorded (nothing was ever received) and no unit test
  fabricates a success response.
- Unknown sports/categories raise `BadRequest` before any request; all 12
  valid catalog cells route through the same single URL to the same typed
  outcome (livescore sits last in every failover order, so the engine skips
  to flashscore/sofascore).

### `pytest -q -m live` run (adapter real-network path)

Run 2026-09-02 through the shipped adapter: **9 passed, 111 deselected** (4
sofascore + 4 flashscore + 1 livescore live tests, exit 0). The livescore test
(`tests/test_livescore_live.py`) made the adapter's real fetch of
`https://www.livescore.com/en/` and **passed by receiving the typed
`SourceUnavailable`** (source="livescore", code="source_unavailable") once the
adapter's retry discipline exhausted its transport attempts; the test asserts
the detail quotes this build's evidence ("2026-09-02", "TCP connect timed
out"). That typed error IS the adapter's verified live behavior this build —
no data rows were ever produced or claimed for this source.

Verdict: livescore is blocked at the edge for this client on this network as
of 2026-09-02 (SPEC network row confirmed by this build's own re-probe). The
adapter ships typed `source_unavailable` only, no data path. Revisit only if
a future probe records a real HTTP response — then a reasoning worker must
discover and verify a real data path before any parse ships.

## Build-time consolidation summary (2026-09-02)

One row per source. All evidence above (this section is a read of it, nothing
new is inferred); verdicts state what returned data at build time and from
which endpoints.

| source | what returned data at build time (2026-09-02) | from which endpoints | verdict |
|---|---|---|---|
| flashscore | football fixtures/results (363 / 226 events at probe), football live rows (truthful statuses, caller-side filtering), basketball day (10 events, Q1-Q4 lines on finished), tennis day (579 rows incl. live with sets) | `2.flashscore.ninja/2/x/feed/f_<sportId>_<offset>_3_en_2` (sportId 1/2/3, offset -1/0/+1 vs the server's Prague date, header `x-fsign`) | **working data path** — day-feed serves the "fixtures/results/live" cells with truthful mixed statuses + standing caller-side-filtering warning; no live clock, no football score_lines (both disproven on probe); h2h is a typed-error cell (df_hh verified per-event-id only) |
| sofascore | live for all 3 sports (football 144 events parsed through the adapter's own path; tennis 92; basketball valid-empty), team-scoped fixtures/results for football + basketball (30 events each; search-resolved) | `www.sofascore.com/api/v1/sport/{football,basketball,tennis}/events/live`, `/team/{id}/events/next|last/0` + `/search/all?q=` — IPv4-forced, session-verified address 151.101.175.52 tried first | **working over IPv4 only** — live (3 sports) and team-scoped fb/bb fixtures/results; date-only fixtures/results, tennis fixtures/results and h2h are typed-error cells (404 evidence recorded above); route fragile: re-probe + update `SOFASCORE_IPV4_FALLBACKS` if the verified address stops answering |
| livescore | nothing — no HTTP response was ever recorded (2 consecutive TCP connect timeouts this session, plain and `curl -4`, matching the SPEC evidence row) | `https://www.livescore.com/en/` (homepage; not a data endpoint) | **blocked** — typed `source_unavailable` only, no data path ships; `parse_events` raises typed `NoData` on any input; sits last in every failover order |

Also recorded during the docs stage (2026-09-02, end-to-end server check, one
external fetch): `GET /v1/football/fixtures?limit=3` on the running service
returned HTTP 200 with 3 parsed flashscore events (source=flashscore, feed
`f_1_0_3_en_2`) and the standing caller-side warning — the engine path
(cache → rate limit → breaker → failover → envelope) works end to end against
the flashscore data path. See docs/api-contract.md for the captured body.

## Fresh-network verification run (2026-09-03, post-build)

Re-run of the whole adapter live suite + a real server session from this
network the morning after the build, one static Chrome UA, sequential
requests (the pytest `-m live` suite's own pacing), uvicorn on :8097.
Purpose: confirm the build-time verdicts hold on a new day (new DNS answers,
new Prague date — today is `f_*_0_3_en_2`'s own day) before the
primepredict_algo integration proceeds.

| check | observed | verdict |
|---|---|---|
| `.venv/bin/pytest -q -m live` (9 tests: 4 sofascore + 4 flashscore + 1 livescore) | **9 passed, 142 deselected** in 50.4 s, exit 0 | **PASS** — all three sources behave exactly as the build-time rows record (sofascore over IPv4 incl. 151.101.175.52 route; flashscore feeds; livescore typed `source_unavailable`) |
| `GET /health` on running service (:8097) | HTTP 200, `status: ok`, all breakers `closed`, 0 calls (fresh process) | **PASS** — breaker surface works |
| `GET /v1/football/fixtures?date=2026-09-03` | HTTP 200, source=flashscore, **50 events** (limit default), first row ES Setif vs Ben Aknoun (Ligue 1, scheduled 2026-09-03T18:00:00Z), latency_ms 1009, cached false | **PASS** — real data for the new date through the full engine path (cache → rate limit → breaker → failover) |
| `GET /v1/tennis/h2h?entity_a=Nadal&entity_b=Djokovic` | HTTP 502, error body `source_unavailable`, `tried_sources: [sofascore, flashscore, livescore]`, livescore detail quotes the 2026-09-02 evidence rows | **PASS (as designed)** — the known coverage-gap cell fails honestly with tried-sources and cited evidence; this is the next extension target |
| server shutdown | TaskStop clean | — |

Nothing new was probed beyond these shipped paths (the ban-risk policy's
volume budget applies: this whole session was ~20 requests total across 3
hosts). Verdict: build-time source-health holds on 2026-09-03; the service
serves real, dated, honest data end to end from this network.

## BetExplorer odds (2026-09-08 upgrade)

New odds category for football/basketball/tennis. Probe session 2026-09-08
(Kali, this network, static browser UA, curl -4, sequential >= 1 s apart).

### Probe table (2026-09-08)

| source | host/path(s) tried | live status | response shape snippet | verdict | fixture recorded |
|---|---|---|---|---|---|
| betexplorer | `www.betexplorer.com/` (home) | 200 | 558 KB HTML with `data-odd` buttons | **works** (index sanity) | — |
| betexplorer | `www.betexplorer.com/football/` | 200 | 476 KB; 213 match rows with `data-dt`, 1X2 `data-odd` triples | **works** | tests/fixtures/betexplorer_football.html |
| betexplorer | `www.betexplorer.com/basketball/` | 200 | 153 KB; 2-way `data-odd` pairs (`my_selections_click('ha',...)`) | **works** | tests/fixtures/betexplorer_basketball.html |
| betexplorer | `www.betexplorer.com/tennis/` | 200 | 309 KB; 2-way `data-odd` pairs | **works** | tests/fixtures/betexplorer_tennis.html |

### What ships as data paths vs typed errors

* **Ships**: `GET https://www.betexplorer.com/{sport}/` for the `odds`
  category only (the site's own next-matches-today page). Parse: match rows
  `data-dt="D,M,YYYY,H,MM"` (kickoff, Europe/Prague assumed — standing
  warning), team link anchor text (`Home - Away`), `data-odd` columns
  (football=3 → 1x2; basketball/tennis=2 → home_away), `matchid=`/href slug
  as `source_event_id`, `js-tournament` header as competition. The odds
  column is BetExplorer's default odds display, recorded as
  `bookmaker="betexplorer-default"` — observed market evidence, never a named
  execution bookmaker.
* **Typed errors**: non-odds categories and non-today dates raise `NotFound`
  (no verified endpoint shape); non-sport raises `BadRequest`; 403/429 →
  `SourceBlocked`/`RateLimited` (breaker trip); rows whose odds count or
  decimal validity fails are skipped with counted warnings (never guessed).

### Live verification (2026-09-08, via running service on :8097)

* `GET /v1/football/odds?limit=3` → 200, source=betexplorer, 3 quotes
  (K League 1, NPL Western Australia), kickoff converted to UTC.
* `GET /v1/basketball/odds?limit=2` → 200, home_away quotes (Yokohama etc.).
* `GET /v1/tennis/odds?limit=2` → 200, home_away quotes (Hipfl N. vs Barton H.
  etc.).
* Offline gates re-run green: pytest 157 passed/9 deselected, ruff clean,
  mypy clean.

## Betika detailed odds (2026-09-08, HTFT + correct score)

Probe session 2026-09-08 (Kali, this network, static browser UA, sequential
>= 1 s apart). Betika is the only probe-verified scraping path carrying
half-time/full-time and correct score markets.

### Probe table (2026-09-08)

| source | host/path(s) tried | live status | response shape snippet | verdict |
|---|---|---|---|---|
| betika | `api.betika.com/v1/uo/matches?page=1&limit=100&tab=upcoming&sub_type_id=1` | 200 | upcoming list, mixes sports (`sport_name` in Soccer / Zoom Soccer / eSoccer / Tennis / Table Tennis / Ice Hockey) | **works** (caller-side Soccer filter) |
| betika | `api.betika.com/v1/uo/match?parent_match_id=<id>` | 200 | market groups: 1=1X2, 10=double chance, 18=total (`special_bet_value total=X`), 45=correct score (0:0..4:4 + OTHER, 26 outcomes), 47=HALFTIME/FULLTIME (9 outcomes) | **works** |

### What ships as data paths vs typed errors

* **Ships**: `odds_detailed` category (football only) → Betika list +
  bounded detail requests (`MAX_DETAIL_REQUESTS=12`). Markets emitted per
  match: `1x2`, `htft` (all nine), `correct_score` (only when the full 0..4
  grid + OTHER is present — completeness attested, never assumed),
  `total_2_5`, `double_chance` (1/X→1X, X/2→X2, 1/2→12). Virtual sports
  (Zoom Soccer / eSoccer) are excluded. Kickoffs parsed Africa/Nairobi →
  UTC with a standing warning.
* **Typed errors**: non-football sports, non-`odds_detailed` categories and
  date params raise `NotFound` (no verified shape); 403/429 → breaker trip.

### Live verification (2026-09-08, via running service on :8097)

`GET /v1/football/odds_detailed` → 44 quotes: 1x2 ×13, double_chance ×11,
correct_score ×6 (26 outcomes each), total_2_5 ×9, htft ×5 — including Club
Brugge vs Aston Villa and AEK Athens vs LASK with all nine HTFT outcomes.

### Excluded candidates probed and rejected (2026-09-08)

| source | result | note |
|---|---|---|
| BetExplorer match pages | only 1x2/ou/ah/dc/bts tabs (`VALID_BET_TYPES` in site JS) | no HTFT/correct score anymore |
| Sofascore odds API | market groups 1 (1X2) and 5 (double chance) only | no HTFT/correct score exposed |
| OddsPortal | React SPA, no server-rendered odds | not scrapeable with plain httpx |
| Oddspedia | Cloudflare challenge ("Just a moment...") | blocked |
| 1xBet LineFeed | 302 geo-redirect | blocked |
| Pinnacle guest API | sports 200 but leagues/markets 204 | geo-limited |
