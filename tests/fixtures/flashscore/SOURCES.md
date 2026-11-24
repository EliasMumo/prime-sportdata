# Flashscore recorded fixtures — provenance (2026-09-02)

All files below are **verbatim recorded HTTP responses or slices of them**. No
content was invented or edited; "trim" means whole contiguous byte ranges cut
on `~` row-boundaries from a larger recorded body. The Stage 4 probe session
(2026-09-02) recorded the slices; the full per-event h2h body was recorded on
2026-11-24 (see the h2h rows). Fetch chain that produced them:

1. `GET https://www.flashscore.com/football/` → HTTP 200 (737 KB app shell;
   no server-rendered rows — evidence the data path had to be discovered).
2. JS chunks on `static.flashscore.com` (webpack core) exposed the feed
   client: `x-fsign: SW9D1eZo`, project id `2`, feed resolver
   `https://2.flashscore.ninja` (list `feed_resolver.local` in
   `core_2_2314000000.js`).
3. All feed bodies below: `GET https://2.flashscore.ninja/2/x/feed/<feed>`
   with one static Chrome UA + `x-fsign` header → HTTP 200, `text/plain`.

Response bodies are UTF-8 pipe format: field sep `÷`, pair sep `¬`, row sep
`~`. Day feeds start `SA÷<sportId>¬~` (soccer=1, tennis=2, basketball=3), then
league blocks `~ZA÷<name>¬ZEE÷<tournamentId>¬ZY÷<country>¬...` interleaved
with match rows `~AA÷<eventId>¬AD÷<unix start>¬AB÷<status>¬AE÷<home>¬AF÷<away>
¬AG÷<home score>¬AH÷<away score>¬...`.

| file | recorded URL | live status | trim note |
|---|---|---|---|
| football/day_today.txt | `https://2.flashscore.ninja/2/x/feed/f_1_0_3_en_2` | 200 | trimmed from a 620 759 B body (363 rows / 97 leagues) to 4 whole league sections containing one row each of AB=1 (scheduled), AB=2 (live), AB=3+AC=4 (postponed), AB=3 finished (KxrZoe94 = Sassuolo–Frosinone, cross-verified vs the sofascore live fixture of the same kickoff) |
| basketball/day_today.txt | `https://2.flashscore.ninja/2/x/feed/f_3_0_3_en_2` | 200 | whole 6 657 B body verbatim (10 rows: 7 finished, 2 postponed, 1 scheduled) |
| tennis/day_today.txt | `https://2.flashscore.ninja/2/x/feed/f_2_0_3_en_2` | 200 | trimmed from a 867 209 B body (579 rows) to the 9 rows of the `ATP - SINGLES: US Open (USA), hard` section (4 finished incl. one 5-setter, 4 live, 1 scheduled) |
| h2h/df_hh_1_KxrZoe94.txt | `https://2.flashscore.ninja/2/x/feed/df_hh_1_KxrZoe94` | 200 | head slice (2 543 B) of the per-event h2h feed; kept as the original 2026-09-02 evidence slice (target row `KP`=KxrZoe94, KL÷2:1, KM÷(1:1), RPA/RPB÷4/3) |
| h2h/df_hh_1_KxrZoe94.full.txt | `https://2.flashscore.ninja/2/x/feed/df_hh_1_KxrZoe94` | 200 | **whole 54 147 B body** recorded 2026-11-24: the same target row (`KP`=KxrZoe94, Sassuolo 2:1 Frosinone, `KC`=1788354000) plus 14 real prior Sassuolo–Frosinone meetings across repeated `Last matches`/`Head-to-head` sections. This is the positive-path regression fixture parsed by `parse_h2h` in `tests/test_flashscore_h2h.py` |

Also probed live that session (evidence rows in docs/source-health.md; bodies
kept in probe scratch, not committed): `sys_1` → 200; `f_1_-1_3_en_2`
(yesterday, 226 rows, all AB=3) → 200; `f_1_1_3_en_2` (tomorrow, 184 rows,
all AB=1) → 200; one malformed URL (empty event id) returned a 1-byte body
`0` — excluded from evidence. No 4xx/5xx/timeouts were observed against
`2.flashscore.ninja` this session.
