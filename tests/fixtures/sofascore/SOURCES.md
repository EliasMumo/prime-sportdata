# Sofascore recorded fixtures — provenance sidecar

Every file here was fetched live on **2026-09-02** from this network (IPv4,
one static realistic UA, sequential, >= 1 s apart) and is a verbatim recording
of the response body, except where "trimmed" is noted (large live payloads were
trimmed to the first N events that maximize status-type variety; the original
event count is recorded below). No file content was invented or edited beyond
array trimming. Probe session + verdicts: `docs/source-health.md`.

| fixture | source URL | status | fetch date | note |
|---|---|---|---|---|
| football/live.json | https://www.sofascore.com/api/v1/sport/football/events/live | 200 | 2026-09-02 | trimmed 85 → 3 events (Coppa Italia inprogress etc.) |
| football/team_next.json | https://www.sofascore.com/api/v1/team/2817/events/next/0 | 200 | 2026-09-02 | FC Barcelona upcoming; trimmed 30 → 3 (notstarted) |
| football/team_last.json | https://www.sofascore.com/api/v1/team/2817/events/last/0 | 200 | 2026-09-02 | FC Barcelona recent; trimmed 30 → 3 (finished) |
| basketball/live_empty.json | https://www.sofascore.com/api/v1/sport/basketball/events/live | 200 | 2026-09-02 | verbatim 13 B `{"events":[]}` — valid empty |
| basketball/team_next.json | https://www.sofascore.com/api/v1/team/3540/events/next/0 | 200 | 2026-09-02 | Real Madrid (basketball); trimmed 30 → 3 |
| basketball/team_last.json | https://www.sofascore.com/api/v1/team/3540/events/last/0 | 200 | 2026-09-02 | Real Madrid (basketball); trimmed 30 → 3 (finished, full Q1-Q4 periods) |
| tennis/live.json | https://www.sofascore.com/api/v1/sport/tennis/events/live | 200 | 2026-09-02 | trimmed 92 → 4 (player events, sets in homeScore.periodN) |
| search/search_all_barcelona.json | https://www.sofascore.com/api/v1/search/all?q=barcelona | 200 | 2026-09-02 | trimmed to first 8 `results[]` rows |
| errors/not_found_scheduled.json | https://www.sofascore.com/api/v1/sport/football/scheduled-events/2026-09-02 | 404 | 2026-09-02 | verbatim 44 B JSON — wrong-path evidence |
| tennis/team_player_404.json | https://www.sofascore.com/api/v1/team/462177/events/next/0 | 404 | 2026-09-02 | verbatim — tennis players not under /team/ |
| h2h/event_duel_aggregate.json | https://api.sofascore.com/api/v1/event/16860534/h2h | 200 | 2026-09-02 | verbatim 102 B aggregate duel summary (no meeting list; no side names) |

Bodies that appear in unit tests but have **no fixture file** (constructed
inline in test code) are synthetic: 403/429/5xx/HTML-challenge bodies and
JSON-error bodies used to test typed-error mapping of the fetch layer — no
such response was observed live this session (every live status is recorded
above).
