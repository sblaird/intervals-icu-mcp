# Session Resume — intervals.icu MCP connector

**Last updated:** 2026-07-07 · **Repo:** `C:\Users\steph\intervals-icu-mcp` · **Branch:** `main`

Read this first to resume.

## 🔀 2026-07-07: PLATFORM PIVOT — moving the chat surface off claude.ai onto GravelFit

Decision: stop fighting the claude.ai tool-surfacing bug (below). The conversational surface
moves to **GravelFit** (`C:\Users\steph\gravelfit` — Vercel React SPA + Fly.io FastAPI backend),
which will call the **Anthropic Messages API MCP connector** pointed at this Cloud Run server.
intervals.icu stays the canonical calendar/workout display via deep links. The claude.ai
connector's Google OAuth flow is KEPT as a fallback surface.

Full design: `docs/specs/2026-07-07-unified-coach-platform-design.md` (approved). Roadmap:
- **Phase 0 (this repo):** commit LEAN_TOOLS work; add static `MCP_SERVICE_TOKEN` bearer auth
  (`load_access_token` override on `GoogleGateProvider`, google_oauth.py:222) for
  server-to-server calls from Anthropic; deploy with `LEAN_TOOLS=1`.
- **Phase 1–3 (gravelfit repo):** unified coach chat endpoint (MCP connector, streaming),
  chat-first frontend + Today panel, cleanup. Note: gravelfit prod model
  `claude-sonnet-4-20250514` is retired — migration mandatory.

The LEAN_TOOLS flag now serves a second purpose either way: 16 tool schemas ≈ 3–6K tokens per
Messages API request vs ~15–25K for 55, and a byte-stable tool list keeps prompt caching warm.

### ✅ 2026-07-07: Phase 0 SHIPPED — LEAN + service token live on Cloud Run

Committed: `49b76fc` (LEAN_TOOLS), `567dde1` (MCP_SERVICE_TOKEN bearer auth + 7 tests).
Quality gate green: **288 tests, ruff + pyright clean.** Deployed revision
**`intervals-mcp-00022-lvm`** (100% traffic) with `LEAN_TOOLS=true` +
`--update-secrets=MCP_SERVICE_TOKEN=mcp-service-token:latest`.

Verification passed:
- Unauthenticated `POST /mcp` → **401**.
- Boot log: `Static service-token auth enabled: True` (+ Firestore store, Google gate, 1 email).
- `tools/list` with the bearer token → **exactly the 16 LEAN_CORE_TOOLS**.

**Auth model now:** `GoogleGateProvider.load_access_token` (google_oauth.py) accepts a static
`MCP_SERVICE_TOKEN` (≥32 chars, constant-time compare) for server-to-server calls (Anthropic MCP
connector / GravelFit backend), else falls through to the Google-OAuth store. Fail-closed when unset.

**⚠️ GOTCHA — secret must have NO trailing newline/CR.** The first `gcloud secrets create` used
`python -c print(...) | tr -d '\n'`, but Windows Python prints `\r\n`, so version 1 kept a stray
`\r` (65 bytes). The *server* survives via `.strip()`, but a client sending the raw value emits an
illegal HTTP header `Bearer ...\r` and fails. **Fixed:** secret **version 2 is clean (64 bytes)**.
When wiring Fly.io in Phase 1, read the token defensively: `gcloud secrets versions access latest
--secret=mcp-service-token --project=intervals-mcp-2026 | tr -d '\r\n'`. Effective token value is
the 64-char urlsafe string (server strips, so v1 and v2 authenticate the same client token).

**Still manual (owner):** confirm the claude.ai OAuth connector still authenticates (open a chat,
sign in as stephen@bramblepathdigital.com) — server side is proven, this is the platform regression.

### ✅ 2026-07-07: Phase 1 curl gate PASSED — connector shape pinned

Live end-to-end call: Anthropic Messages API → MCP connector → Cloud Run server → intervals.icu.
Claude (`claude-opus-4-8`) invoked `get_fitness_summary` and `get_athlete_profile` server-side and
returned live data (athlete FergusYL / `i29347`). Confirmed request shape for the GravelFit backend:

- **Beta header:** `anthropic-beta: mcp-client-2025-11-20` (NOT the legacy `2025-04-04`).
- **Two required halves:** `mcp_servers: [{type:"url", url, name:"intervals-icu", authorization_token:<MCP_SERVICE_TOKEN>}]`
  **plus** `tools: [{type:"mcp_toolset", mcp_server_name:"intervals-icu"}]`. Omitting the toolset = 400.
- **Auth:** `authorization_token` is forwarded as `Authorization: Bearer` — our static service token works.
- **Stream/response block types** (for the SSE mapper in `services/unified_coach_ai.py`):
  `mcp_tool_use` (fields `name`, `server_name`) and `mcp_tool_result` (fields `is_error`, `content` =
  list of `{type:"text", text:<json string>}`). `stop_reason: end_turn` on completion; handle `pause_turn`.
- **Token cost:** 16 lean tool schemas ≈ **12.3K input tokens** per call (richer schemas than the 3–6K
  estimate, but still far below the 55-tool manifest). Add `cache_control` on the system prompt so
  repeat turns are cache reads.

**⚠️ Data finding:** `get_fitness_summary` returned `no_data` and `get_athlete_profile` shows
`"fitness":{}` — CTL/ATL/TSB are empty via these paths for athlete i29347 right now (wellness,
profile, activities all return live data). Investigate before wiring the Today panel's load numbers
(FR2): confirm whether fitness needs a date param / recalc on intervals.icu, or use a different field.

### ✅ 2026-07-07: Coach system prompt written + tool-surface decided

**System prompt:** `C:\Users\steph\gravelfit\coach_system_prompt.md` (adapted from the claude.ai
project instructions the user provided). Improvements: corrected the intervals.icu **subjective
scales** (they're mostly inverted 1–4 where 1=best — the old /10 & /5 assumptions were wrong;
fixed every running/fatigue/planning rule that used them), corrected calendar categories
(WORKOUT/NOTE/RACE/GOAL; strength→WORKOUT+WeightTraining), added a "no delete tool" rule, and wove
in the expanded data: `get_power_model` (eFTP/CP/W′), `get_power_vs_hr_trend`,
`get_activity_curves(fatigue=true)` durability, rich fields (`decoupling_percent`,
`variability_index`, `efficiency_factor`, `polarization_index`, `zone_times`), the **fueling audit**
(`carbs_ingested_grams` vs `carbs_used_grams`), and wellness `vo2max`. Deep-link + confirm-before-write
+ real-data-first-with-estimation-fallback baked in. Left the old `fitness_coach_system_prompt.md` for reference.

**Tool-surface decision (user chose "curated coaching set"):** REMOVE `LEAN_TOOLS` from Cloud Run
so the server exposes all 55 (destructive tools stay gated via `ENABLE_WRITE_TOOLS`, still off), and
have the GravelFit backend **allowlist** a coaching subset per-request via the connector's
`tools:[{type:"mcp_toolset", ..., default_config:{enabled:false}, configs:[{name,enabled:true},…]}]`.
Redeploy: `gcloud run deploy intervals-mcp --source . --region=us-central1 --project=intervals-mcp-2026
--remove-env-vars=LEAN_TOOLS` (merge form preserves OAuth secrets + MCP_SERVICE_TOKEN).

Intended allowlist (~35, drop gear/downloads/sport-writes/deletes): all READ (profile, fitness,
sport_settings, recent/details/around/search activities, wellness ×2, calendar ×2, event), all
ANALYZE (intervals, best_efforts, power/hr/pace curves, power_model, power_vs_hr_trend,
activity_curves, streams, 4 histograms, power_vs_hr, time_at_hr, interval_stats, segments), weather ×2,
routes ×3, workout library ×2, `update_wellness`, and calendar writes (create/bulk_create/update/
duplicate/mark_done). Finalize exact names when wiring `unified_coach_ai.py`.

### ✅ 2026-07-07: Cloud Run full surface + GravelFit backend built & verified

- **Cloud Run:** LEAN_TOOLS removed, revision **`intervals-mcp-00023-bvn`** exposes all **55**
  tools (destructive still gated). Verified via authenticated tools/list.
- **GravelFit backend** (`C:\Users\steph\gravelfit`, committed locally `c4fae09`, NOT pushed):
  new `backend/services/unified_coach_ai.py` (`UnifiedCoachAI`) streams via
  `client.beta.messages.stream(betas=["mcp-client-2025-11-20"], mcp_servers=[...], tools=[mcp_toolset])`,
  curated ~42-tool allowlist, maps `mcp_tool_use`/`mcp_tool_result` → existing SSE frames
  (text/tool_call/tool_result/done), handles `pause_turn`, loads `coach_system_prompt.md` with
  `cache_control`, model `claude-opus-4-8`. Rewired `routers/coach.py` (`/chat`, `/chat/stream`)
  to the new service; added `mcp_server_url`/`mcp_service_token` to `config.py`; bumped anthropic
  pin `>=0.77.0` (installed 0.77.0 supports the connector). Frontend unchanged.
- **Smoke test (live, local venv):** streamed text + `get_athlete_profile` server-side →
  FergusYL/i29347; frame counts text×4/tool_call×1/tool_result×1/done×1. py_compile clean.

**⚠️ GOTCHA — `mcp_toolset.configs` is an OBJECT, not a list.** The claude-api skill showed
`configs: [{name, enabled}]`; the API rejects that (400 "Input should be an object"). Correct
shape: `configs: {"<tool_name>": {"enabled": true}, …}` with `default_config: {"enabled": false}`.

### ✅ 2026-07-07: Phase 2 frontend built (chat-first Coach + Today panel)

GravelFit committed locally `d79ccb5` (NOT pushed). Vite build clean (420 modules).
- Backend `GET /api/today` (`backend/routers/today.py`, registered): readiness (HRV/RHR/sleep) +
  CTL/ATL/TSB + next planned workout, sourced from intervals.icu via the caching client, **DB-free**.
  **Verified with real data:** CTL 49.5 / ATL 53.6 / TSB −4.1, HRV 24, RHR 53, sleep 7.4h, next
  "Z2 Easy" today.
- Frontend: `pages/Coach.jsx` (reusable `components/coach/CoachChat.jsx` + `components/today/TodayPanel.jsx`
  side rail), index route `/` → `/coach`, sidebar gains Coach + external intervals.icu Calendar/Fitness
  links, floating FitnessCoach slide-over suppressed on `/coach` (no double chat). Legacy pages kept
  (Phase 3 cleanup). FitnessCoach.jsx left untouched (zero regression); CoachChat duplicates its SSE
  logic — consolidate in Phase 3.

**🔑 KEY FINDING (CTL/ATL/TSB "empty" resolved):** the numbers DO exist. GravelFit's direct
`intervals_client.get_fitness()` reads CTL/ATL/TSB from the **wellness rows** and returns real values
(49.5/53.6/−4.1). Only the MCP server's `get_fitness_summary` + `profile.fitness` paths return empty.
So the coach can source load from `get_wellness_data`/`get_wellness_for_date` (wellness carries
ctl/atl/tsb) even while `get_fitness_summary` is broken. Fix path for the MCP server: make
`get_fitness_summary` read from the wellness/PMC source the direct client uses. (Deferred by user.)

### ✅ 2026-07-07: Fly.io backend DEPLOYED & verified live

Deployed to `gravelfit-backend.fly.dev` (staged `MCP_SERVICE_TOKEN` applied; ANTHROPIC/INTERVALS
keys already present). **Deploy gotcha:** run `flyctl deploy` from the **gravelfit repo ROOT**
(root `fly.toml` → `[build] dockerfile = "backend/Dockerfile"`, Dockerfile COPYs `backend/…`).
Running from `backend/` uses the wrong `backend/fly.toml` and fails ("/backend not found").

Verified live:
- `GET /api/today` → real data (CTL 49.5 / ATL 53.6 / TSB −4.1, HRV 24, RHR 53, sleep 7.4h, next Z2 Easy).
- `POST /api/coach/chat/stream` → full chain Fly → Anthropic → MCP connector → Cloud Run → intervals.icu:
  streamed text + `get_athlete_profile` server-side → FergusYL/i29347; frames text/tool_call/tool_result/done.

**Minor follow-up:** legacy `coach_chats` history references the old client-side tools (e.g.
`propose_week_plan`), which mildly confuses the new coach on replay. A one-time
`DELETE /api/coach/history` gives a clean slate; otherwise self-resolves as new turns accumulate.

### ✅ 2026-07-07: Phase 3 cleanup DONE + two production bugs caught & fixed

CTL/ATL/TSB prompt fix, backend dead-code removal (coach.py 611→~140), model retirement
completed (`fitness_coach_ai.py` sonnet-4→**sonnet-5**; kept because nutrition/planning routers
use it), frontend chat consolidation (`FitnessCoach` → thin shell over shared `CoachChat`), README
architecture + token-rotation runbook. gravelfit commits `2a59aa4`, `971b688`, `bcc01c6` (pushed).

**🐛 BUG 1 (critical) — system prompt was NEVER in the container.** `coach_system_prompt.md` sat at
the gravelfit repo ROOT, but the Dockerfile only `COPY backend/ .`, so it wasn't in the image →
`UnifiedCoachAI` silently used its 4-line fallback prompt in prod the whole time (local smoke test
loaded the real file and masked it). Fix: **moved to `backend/coach_system_prompt.md`**, load from
`Path(__file__).parent.parent`. Verified live: coach now runs the full 44K prompt — on a CTL/ATL/TSB
question it hit `get_fitness_summary` (empty) → per the fix, said "let me check your wellness rows"
→ `get_wellness_data` → reported the REAL 49.5/53.6/−4 (was hallucinating 15.5 on the fallback).

**🐛 BUG 2 — `tool_calls.map is not a function`.** History rows store `tool_calls` as a JSON string;
the chat render called `.map()` on it → crashed the chat whenever a past turn had a tool call
(pre-existing, exposed by the consolidation). Fix in `CoachChat`: parse on load + `Array.isArray` guard.

**Browser E2E (Playwright) all green:** `/coach` loads history with tool chips (no crash); `/dashboard`
slide-over opens cleanly (shared CoachChat, Close button); Today panel real data; Dashboard + legacy
pages intact (kept deliberately — they have unique check-in/weight/nutrition/journal features).

**MIGRATION COMPLETE.** Optional future work: consolidate nutrition/planning routers onto the
unified coach.

### ✅ 2026-07-07: Date-grounding fix (stale race countdowns)

Bug: coach called the already-completed Muddy Onion race an "A race 27 days out" (27 days maps to a
frozen ~2026-03-30 context). Root cause: the unified coach had NO current-date grounding — the planned
"today is X" injection was never implemented — so it carried stale relative countdowns forward from
the rolling 20-msg chat window. (The "context block" it cited is NOT injected by the coach path; the
stale 27/139-days figures trace to the legacy planning router's DB context / seed. Coach was
confabulating timing without a date anchor.) Fix (gravelfit `20cbb02`, deployed): every user turn is
prefixed server-side with the athlete's local date (`_ground_in_today`, America/New_York), marked
authoritative; prompt §0 now requires computing all timing from that date + a live `get_calendar_events`
read and treating past-dated events as completed. **Verified live:** "Is Muddy Onion coming up?" →
reads calendar → "already behind you, ~10+ weeks ago; next race Vermont Overland Aug 15, 39 days out."

### ✅ 2026-07-07: Persistent athlete context SHIPPED (hybrid: auto facts + coach notes)

Every coach turn now injects an `## ATHLETE CONTEXT` block (via `_ground_in_today`, after the date):
- **Auto facts** (`services/athlete_context.py::build_athlete_context`) rendered live each turn from
  the DB + cached intervals.icu, with ABSOLUTE dates + days-to-today so nothing goes stale: races
  (events table — Muddy Onion shown "2026-04-25, 73 days ago, COMPLETED"; Vermont Overland "in 39
  days"), active training block, current CTL/ATL/TSB, weight goal. All guarded.
- **Coach notes** — new `coach_context` table (migration `008`), single row. Coach updates via a new
  client-side `update_coach_notes` tool (mixed with the MCP toolset; handled in a `stop_reason ==
  "tool_use"` continuation loop in `unified_coach_ai`). Router builds context + a note_writer bound to
  the db client each turn.
**Verified live:** "remember my Achilles issue" → coach called `update_coach_notes` (saved); next turn
recalled it from the injected context (persists across sessions, not just chat history).

**🚨 INCIDENT (self-inflicted, fixed):** migration `008` had a **semicolon inside a SQL comment**; the
naive `run_migrations` splits on `;`, so it split the comment into invalid SQL (`near "this": syntax
error`), `init_db()` threw, and **app startup crashed (Fly machine exited)** — brief outage. Fix
(`2ff581d`): no semicolons in migration comments. Redeployed; `/api/today` healthy, migration applied.
Lesson for future migrations: this repo's runner is a dumb `split(';')` — keep `;` out of comments.

Commits: gravelfit `27a1da9` (feature), `2ff581d` (migration fix). Both pushed + deployed.

### ✅ 2026-07-07: calendar deep-link fix + review write-back (gravelfit `2f09b25`)

1. **Calendar links** now use the working week-stamp form `https://intervals.icu/?w=<Monday>` (bare
   `/calendar` doesn't load a week). Shared `frontend/src/utils/intervals.js::intervalsWeekUrl`; fixed
   in Today panel, sidebar, `/api/today` (`_week_url`), and the coach prompt. Verified live:
   `/api/today` → `?w=2026-07-06`.
2. **Review write-back:** added `update_activity` to the coach allowlist (44 tools); prompt §8A now
   writes an `ATHLETE FEEDBACK / COACH REVIEW / DECISION` block to the activity's `description` after a
   review (preserving existing notes), exempt from the calendar-write confirm. Not live-tested (writes
   to a real activity + slow review turn) — takes effect on next review.

Note: `Updates/Anthropic key.txt` is the API key (gitignored), NOT a spec — do not process/move it.

### ✅ 2026-07-07: item-3 features SHIPPED (gravelfit `201b828`) — proactive nudges + eFTP panel

User picked two of four proposed features:
1. **Proactive nudges** (prompt §0): coach briefly flags taper timing (race dates in context), fueling
   (carbs_ingested vs carbs_used after long rides), PRs, and eFTP-vs-FTP drift when data warrants.
2. **eFTP on Today panel**: new `intervals_client.get_ftp()` reads `/mmp-model?type=Ride` (`.ftp` =
   eFTP) + athlete `sportSettings[Ride].ftp` (configured). `/api/today` returns
   `ftp:{configured, eftp, stale}` (stale = diverge ≥5%). Panel shows "<eftp> eFTP · set <configured> ·
   re-test due". **Verified live in browser:** "279 eFTP · set 300 · re-test due" (real 7% gap — set
   FTP is above modeled, so %-targets run hot). Calendar week-URL fix also confirmed live (next-workout
   link → `?w=2026-07-06`).

Not built (available later): gear maintenance reminders; one-tap daily readiness brief.

**App is fully live and healthy.** Frontend `https://frontend-two-alpha-22.vercel.app`, backend
`gravelfit-backend.fly.dev`, MCP `intervals-mcp-840283109221.us-central1.run.app`.

### ✅ 2026-07-08: PDC-freshness nudge (gravelfit `4cfc7fc`)

Added power-duration-curve freshness to the athlete context. `intervals_client.get_ride_power_curve()`
fetches `/power-curves?type=Ride` (parallel `secs`/`values`/`activity_id` arrays + `activities` map);
`athlete_context._pdc_freshness()` finds, per band (5s neuromuscular / 1min anaerobic / 5min VO2 /
20min threshold), the best watts and how old the setting activity is, flagging STALE at ≥56 days.
Prompt §0 nudge (e): when a band is stale, proactively prompt a specific targeted max effort. **Finding:
this athlete's whole PDC is 310-358 days old** (5s 1056W/310d, 1min 512W/358d, 5min 359W/318d, 20min
309W/333d) — so eFTP/zones/%-targets are all derived from ~year-old efforts. **Verified live:** "what
should I prioritize?" → coach proactively flagged "entire PDC stale, every band 300+ days old, a problem
38 days from Vermont Overland" (no tool call — data is in the injected context).

### ✅ 2026-07-07: Workout-review skill folded into the coach

Adapted the `workout-review` skill (user-supplied `.skill` bundle) into `coach_system_prompt.md`
section 8A: 3-dimension compliance scoring (power/duration/pacing), scorecard + block-by-block
output, longitudinal comparison, race-pace mode, low-score diagnosis. Adaptations for this app:
**intervals.icu-only** (skill's Strava tools remapped to `search_intervals` / `get_activity_segments` /
`compare_route_similarity`) and **no code execution** (VI = normalized_power ÷ average_watts from
`get_activity_intervals`, not numpy). Added `search_intervals` to the coach allowlist (now 43 tools).
gravelfit `f7e2c51`, pushed + Fly-deployed. **Live-tested:** "Review my most recent ride" →
called get_recent_activities/get_activity_details/get_activity_intervals, produced a compliance
scorecard with all three dimensions (done frame received, 2.5K-char review).
**UX note:** a full review is a slow turn (~3-4 min — several tool calls + detailed analysis on
opus). Fine in the browser (SSE streams to completion, no hard timeout); just slower than a normal
reply. Chosen approach was "fold into system prompt" (vs on-demand injection / Anthropic Agent Skills).

### ✅ 2026-07-07: `get_fitness_summary` wellness fallback SHIPPED (rev 00024)

Fixed the long-standing empty CTL/ATL/TSB: `tools/athlete.py::get_fitness_summary` read only the
athlete-profile fitness object (empty for this account). Now when profile CTL/ATL are None it falls
back to `_latest_fitness_from_wellness()` — most recent wellness row (30-day window, sorted by date),
reads `ctl`/`atl`/`rampRate`, derives TSB as ctl−atl. Adds `metadata.source` (athlete_profile|wellness).
Commit `84e5d16`; 290 tests pass (2 new: fallback + genuinely-empty→no_data); ruff/pyright clean.
Deployed `intervals-mcp-00024-6p2` (code-only, secrets preserved). **Verified via MCP connector:**
`get_fitness_summary` → CTL 49.5 / ATL 53.6 / TSB −4.1, `source: wellness` (was `no_data`). The
coach's primary fitness tool now returns real load directly; the prompt's wellness-fallback line is
now harmless belt-and-suspenders. (Note: `get_athlete_profile.fitness` still shows `{}` — same
fallback could be applied there, but the coach uses get_fitness_summary.)

### ✅ 2026-07-07: SHIPPED — full stack live & E2E-verified in browser

Both repos pushed to GitHub (`sblaird/gravelfit`, `sblaird/intervals-icu-mcp`); legacy `coach_chats`
history cleared (4 rows). Vercel auto-deployed the frontend. **Live URL:
`https://frontend-two-alpha-22.vercel.app`** (root redirects to `/coach`; the two `gravelfit.vercel.app`
domain guesses 404 — this alpha-22 domain is the real one). Browser E2E (Playwright) passed:
- `/` → `/coach`; sidebar Coach/Dashboard/Nutrition/Journal + intervals.icu Calendar↗/Fitness↗ links.
- Today panel real data: HRV 24 / RHR 53 / sleep 7.4h; CTL 49.5 / ATL 53.6 / TSB −4.1 "Neutral";
  next "Z2 Easy · 45 TSS" → intervals.icu/calendar.
- Sent a chat message → streamed a full coaching reply with "Read fitness" + "Read activities" tool
  chips. Full stack confirmed: Vercel → Fly → Anthropic MCP connector → Cloud Run → intervals.icu.

**Confirmed the CTL/ATL/TSB gap in the coach's own words:** it said load metrics "aren't currently
populating" because it used `get_fitness_summary` (empty) — while the Today panel (direct client,
reads wellness) shows the real 49.5/53.6/−4.1. **Cheapest fix (deferred): one system-prompt line** —
"if `get_fitness_summary` returns no data, read CTL/ATL/TSB from `get_wellness_data` (wellness rows
carry ctl/atl/tsb)." A proper fix is to make the MCP server's `get_fitness_summary` read the same
wellness/PMC source the GravelFit direct client uses.

**Remaining — Phase 3 cleanup only:** retire `services/fitness_coach_ai.py` + `execute_tool` in
`routers/coach.py`; consolidate the duplicated `CoachChat`/`FitnessCoach` SSE logic; delete unrouted
legacy pages (Dashboard/Calendar/Nutrition/Journal if truly unused) + prune dead `api/client.js`
fetchers; slim `intervals_client.py` to Today-panel needs; README architecture + token-rotation runbook.

### ⏸ (resolved) Fly.io deploy needed `flyctl auth login` — done

flyctl v0.4.27 installed at `~/.fly/bin/flyctl` but NOT authenticated ("no access token"). This is an
interactive browser flow the agent can't drive. Once the user runs `flyctl auth login`, deploy with:
```
FLY=~/.fly/bin/flyctl
"$FLY" secrets set MCP_SERVICE_TOKEN=$(gcloud secrets versions access latest --secret=mcp-service-token --project=intervals-mcp-2026 | tr -d '\r\n') --app gravelfit-backend
cd gravelfit/backend && "$FLY" deploy
```
(ANTHROPIC_API_KEY already on Fly; MCP_SERVER_URL has a config default.) Model retirement is already
resolved — chat routes call UnifiedCoachAI (opus-4-8); the retired sonnet-4 model is dead code.

Remaining: Fly deploy (user auth) → push both repos → Vercel preview E2E (browser: chat streams,
Today panel loads, deep links) → Phase 3 cleanup. See `docs/specs/2026-07-07-unified-coach-platform-design.md`.

## ⚠️ 2026-07-07: server/connector HEALTHY, but claude.ai chat won't surface the tools (reconnect needed)

**What's proven healthy (do NOT touch the server/deploy):**
- Direct calls from Claude Code via the `claude_ai / Intervals_icu` connector return real 2026-07-07 data:
  `get_athlete_profile`, `get_recent_activities`, `get_wellness_data` (athlete `i29347`). Server + upstream
  auth + Firestore token store all fine.
- claude.ai **Settings → Connectors → Intervals.icu**: **Connected** (green ✓), type Web/**Custom**, URL
  `https://intervals-mcp-840283109221.us-central1.run.app/mcp`, **55 tools listed, all "Always allow."**

**The actual symptom (claude.ai web side, NOT the server):** claude.ai chats — both inside the Fitness
Assistant Project AND a plain non-project chat — cannot *invoke* the Intervals.icu tools even though the
connector is toggled ON. Ruled out by direct experiment on 2026-07-07:
- NOT a project-scope issue (fails in a clean regular chat too).
- NOT a per-conversation tool-count cap (fails even with **only** Intervals.icu enabled, all other
  connectors off).
- NOT the "Load tools when needed" vs "Tools already loaded" setting (fails in both).
- The chat model's own report is unreliable (it claimed Drive was available while Drive was toggled off),
  but the *consistent* cross-chat inability to call Intervals.icu is real. One chat described it as: the
  connector "is exposed only as something I can wire into an artifact via the Anthropic API — not a tool
  I can invoke directly in chat."

**Conclusion:** the connector's tool manifest isn't being wired into the claude.ai chat tool list — a
platform-side surfacing failure, not a config error. Standard remedy = **disconnect + reconnect** the
Intervals.icu connector at Settings → Connectors so claude.ai re-fetches the manifest.

**TRAP on reconnect (must be the user — OAuth):** the Google-OAuth gate accepts **only
`stephen@bramblepathdigital.com`** (`MCP_ALLOWED_EMAILS`). Reconnecting and signing in as
`stephen.b.laird@gmail.com` is rejected/fail-closed and reproduces "connector exposes nothing." If
reconnect (as the right Google account) still doesn't surface the tools in chat, it's an Anthropic
platform issue for support — the server side is provably correct.

## 🧪 2026-07-07: LEAN_TOOLS flag added to test the claude.ai tool-surfacing failure

Hypothesis: claude.ai's chat surface drops this Custom connector because its **55-tool** manifest
exceeds some chat-side budget (Claude Code loads tools on demand, so it keeps them either way).
`LEAN_TOOLS` (in `server.py`, mirrors the `ENABLE_WRITE_TOOLS` pattern) registers only a **16-tool
coaching core** (`LEAN_CORE_TOOLS`): profile, fitness summary, wellness (+by-date), recent activities,
activity details, intervals, best efforts, power curves, sport settings, calendar read + upcoming, and
the calendar writes (create/update/bulk/mark-done). Strict subset — destructive tools stay out even if
`ENABLE_WRITE_TOOLS` is on. Off by default (identical to before). Gate green: **281 tests, ruff clean,
pyright 0 errors** (`tests/test_lean_tool_set.py`). Verified at runtime: default=55 tools, LEAN=16.

**Deploy to test (SAFE — preserves the R1 Google-OAuth secrets; do NOT use the stale full deploy
command in `project_intervals_mcp.md`, it omits `GOOGLE_OAUTH_*`/`MCP_ALLOWED_EMAILS` and would
crash-loop the fail-closed boot):**
```
gcloud run deploy intervals-mcp --source . \
  --region=us-central1 --project=intervals-mcp-2026 \
  --update-env-vars=LEAN_TOOLS=true
```
`--source .` rebuilds with the new code; `--update-env-vars` MERGES the flag, leaving all existing
env/secrets intact. Needs `gcloud auth login` as `stephen@bramblepathdigital.com` (creds expire).

**After deploy:** claude.ai has the 55-tool manifest cached — force a re-fetch (fresh chat first; if the
tool list doesn't shrink, toggle the connector off/on in the project, or disconnect/reconnect) then ask
the chat to call `get_athlete_profile`.

**Revert to full 55 (once lean code is live, no rebuild needed):**
```
gcloud run services update intervals-mcp --region=us-central1 \
  --project=intervals-mcp-2026 --remove-env-vars=LEAN_TOOLS
```
If lean fixes it → it's a claude.ai per-connector tool-count limit; decide a permanent lean roster or
split into two connectors. If it does NOT fix it → claude.ai platform bug (issue #1675 class); the
server is provably fine and it's an Anthropic support item.

## ⚡ 2026-07-06 session: ALL PHASES (0–4) CODE COMPLETE (not yet deployed)

Every requirement R1–R16 implemented test-first (`f30c4e6`..`1beab4c`), gate green
(**269 tests · ruff clean · pyright 0 errors**). Pushed to origin.

- **R1** Google-identity OAuth gate (`google_oauth.py`; fail-closed boot) · **R2** `ENABLE_WRITE_TOOLS`
  gates 5 deletes + `apply_sport_settings` (default OFF)
- **R3** stream caps (3000 default) · **R4** download guards (5 MB inline, scratch-dir-confined
  output_path) · **R5** similarity `include_paths=False` · **R6** resilient per-item list parsing
  (all 14 sites) + `dropped_count` metadata; Athlete/Event/HistogramBin drift-tolerant
- **R7** Firestore load-retry + critical persist raise · **R8** retry/backoff (429/5xx, Retry-After)
  · **R9** generic upstream error messages · **R10** non-root container (verified in Docker)
- **R11** unified config via middleware ctx · **R12** date-format hardening · **R13** central
  handler-exception logging in `build_error_response`
- **R14** 30 new Activity fields in `get_activity_details` · **R15** wellness `vo2max` · **R16** five
  new tools: `get_power_model`, `get_power_vs_hr_trend`, `get_activity_curves`, `get_interval_stats`,
  `get_activity_segments` (56 → 61 tools)

**DEPLOYED 2026-07-06:** revisions `00019-w56` → `00020-dls` (Google OAuth client created, secrets in
Secret Manager, accessor granted). Verified live: unattended `register → authorize` 302s to
accounts.google.com with no code issued; Firestore `oauth_state/singleton` flushed (deleted → fresh
revision → confirmed 404). ALL `Updates/01–05` archived. Deploy command now carries the two
`GOOGLE_OAUTH_*` secrets + `MCP_ALLOWED_EMAILS=stephen@bramblepathdigital.com`; `ENABLE_WRITE_TOOLS`
unset (deletes off in prod).
**Only remaining step: re-authorize the claude.ai connector once as stephen@bramblepathdigital.com**
(all pre-hardening tokens are dead by design).

---

Below: context from the 2026-07-03→05 sessions — the shipped bug fixes, the connector review, the
hardening plan, the four resolved decisions, and the R1 Google-OAuth console setup.

---

## 1. Current live state (all shipped)

- **Live Cloud Run revision:** `intervals-mcp-00018-bnm` (project `intervals-mcp-2026`, `us-central1`),
  serving 100% traffic, boots clean (public OAuth route returns 200).
- **Git:** `main` == `origin/main` == commit **`ff7e02f`** (pushed).
- **Quality gate:** 135 tests pass · ruff clean · pyright 0 errors.
- **Service URL:** `https://intervals-mcp-840283109221.us-central1.run.app`
- **Athlete:** `i29347`. **Token store:** Firestore (`oauth_state/singleton`), survives deploys.

### What shipped this session (already live)
1. **Issue #1 latlng streams crash** — resilient parsing (already committed earlier; now deployed).
2. **Issues #2/#3 stringified int/array args** — generalized: `widen_tool_schemas_for_string_args(mcp)`
   in `coercion.py`, called once in `server.py`, widened 60 params + all future tools. (`379388e`)
3. **Issue #4 sport-settings** — model field names corrected (`types`/`lthr`/`threshold_pace`, zones);
   pace write path converts human min/km & min/100m → m/s; zone-note corrected. (`ff7e02f`)

### Verified fact — sport-settings units (throwaway experiment on live API, 2026-07-03)
- `threshold_pace` is stored in **meters per second (m/s)**. `pace_units` is display-only.
- `threshold_pace` is **silently dropped on POST create** — must be set via a follow-up PUT.
- Zone arrays differ: `power_zones` = **% of FTP** (trailing 999 = open top zone); `hr_zones` =
  **absolute bpm** (top == max_hr).

---

## 2. The connector review (done)

Three parallel review agents covered **stability, security, data coverage** → 18 findings.
- **HTML report artifact:** https://claude.ai/code/artifact/085ff4d1-546c-4984-b9d7-63da7bc928eb
- **Headline (Critical):** the OAuth flow auto-approves any anonymous client — anyone who finds the URL
  can get a token to all your data + the delete tools. This is what R1 fixes.
- Stability grade B, Security D (until R1), Data B−.

---

## 3. The hardening plan (written, not yet implemented)

All requirements live in **`Updates/`** as phase files (self-contained, acceptance criteria, moved to
`Updates/Archive/` when done per `CLAUDE.md`). Index/traceability in `docs/connector-review-fixes.md`.

| File | Phase | Requirements | Status |
|------|-------|--------------|--------|
| `Updates/01-critical-security.md` | 0 · Critical | R1 (Google-OAuth lock), R2 (gate destructive tools) | **Code done — deploy pending (see §5)** |
| `Updates/Archive/02-stability-high.md` | 1 · High | R3 stream caps, R4 download guards, R5 route-path opt-out, R6 resilient list parsing | **Done 2026-07-06** |
| `Updates/03-medium-hardening.md` | 2 · Medium | R7 Firestore resilience, R8 retry/backoff, R9 generic errors, R10 non-root container | Ready |
| `Updates/04-polish.md` | 3 · Low | R11 unify config, R12 date hardening, R13 handler logging | Ready |
| `Updates/05-data-enrichment.md` | 4 · Data | R14 Activity fields, R15 wellness vo2max, R16 five new tools | Ready |

Phases 1–4 have **no blocking decisions** — can be implemented immediately. Suggested first: Phase 1
(`02-stability-high.md`) — fixes the payload issue that forced the switch to Strava.

---

## 4. Decisions — RESOLVED 2026-07-04

- **D1 (auth):** **Google-identity federation.** `authorize()` federates to Google Sign-In; only emails
  in `MCP_ALLOWED_EMAILS` (allowlist of one) can get a token. No password; no other credential accepted.
- **D2 (write gating):** Gate the **5 deletes + `apply_sport_settings`** behind `ENABLE_WRITE_TOOLS`
  (default off). Keep `create_*`/`update_*`/`mark_event_done`/`bulk_create`/`duplicate` available.
- **D3 (key rotation):** **No** API-key rotation (key was never the leaked credential). Instead, a
  one-time **Firestore token-store flush** (`oauth_state/singleton`) at R1 deploy to kill any token
  from the open window; then re-authorize once.
- **D4 (data):** **Ship everything** — R14/R15 fields + all five R16 tools.

R1 and R2 in `Updates/01-critical-security.md` are already rewritten to match D1/D2/D3.

---

## 5. ⏸ IN-FLIGHT: R1 Google-OAuth client setup (resume here)

R1 needs a Google OAuth 2.0 **Web client** created in project `intervals-mcp-2026`. This is a **Console
task** — there is *no clean `gcloud` command* for a standard Web OAuth client (the `gcloud iap
oauth-clients` path is IAP-branded and the wrong tool). Only the secret storage is `gcloud`.

### PENDING micro-decision (answer to proceed)
Which Google account gates access + consent-screen type:
- **Recommended:** Workspace account `stephen@bramblepathdigital.com` + **Internal** consent screen
  (no test-user/publishing hassle, org-restricted bonus). → `MCP_ALLOWED_EMAILS=stephen@bramblepathdigital.com`
- **Alt:** personal `stephen.b.laird@gmail.com` + **External** consent screen + add self as test user.

### Console steps (project `intervals-mcp-2026`)
1. **APIs & Services → OAuth consent screen** → Internal (or External + add self as test user) →
   name "intervals-mcp connector" → save. Scopes: `openid` + `email` (default, nothing to add).
2. **APIs & Services → Credentials → Create credentials → OAuth client ID → Web application.**
   - **Authorized redirect URI:**
     `https://intervals-mcp-840283109221.us-central1.run.app/auth/google/callback`
     (callback route is built during R1 — registering the URI now is fine)
   - Copy the **Client ID** and **Client secret**.

### Then store the secrets yourself (keeps them out of chat)
```bash
printf '%s' 'PASTE_CLIENT_ID'     | gcloud secrets create GOOGLE_OAUTH_CLIENT_ID     --data-file=- --project=intervals-mcp-2026
printf '%s' 'PASTE_CLIENT_SECRET' | gcloud secrets create GOOGLE_OAUTH_CLIENT_SECRET --data-file=- --project=intervals-mcp-2026
```
Then report **"secrets created"** + the chosen email. The assistant finalizes R1's config to read those
secrets and sets the deploy env. (Was about to offer folding these steps into `Updates/01` as a
prerequisites checklist — do that on resume.)

### gcloud auth note
`gcloud auth login` was done as `stephen@bramblepathdigital.com` on 2026-07-03; it may need refreshing
after reboot (`gcloud auth login` — interactive, run it yourself).

---

## 6. Next actions (resume checklist)

1. [ ] Answer the §5 micro-decision (email + consent-screen type). ← **only remaining blocker**
2. [ ] Create the Google OAuth client (Console) + store the two secrets (`gcloud`, above).
3. [x] Fold the §5 prerequisites into `Updates/01-critical-security.md`. (2026-07-06)
4. [x] Planning docs committed (`1ee7612`).
5. [x] Phase 0 code (R1+R2) + Phase 1 (R3–R6) implemented, committed, gate green. (2026-07-06)
6. [ ] Deploy with new secrets + `MCP_ALLOWED_EMAILS` + `ENABLE_WRITE_TOOLS` unset (needs go-ahead);
       then the one-time Firestore `oauth_state/singleton` flush; re-authorize connector; archive
       `Updates/01`.
7. [ ] `git push` (needs go-ahead). Phases 2–4 (`Updates/03–05`) — unblocked, not started.

---

## 7. Git state / housekeeping

**Untracked (uncommitted) — survive reboot on disk, but commit to be safe:**
- `docs/connector-review-fixes.md` (index/reference)
- `docs/SESSION-RESUME.md` (this file)
- `Updates/01-critical-security.md` … `Updates/05-data-enrichment.md`
- (`Updates/Archive/` exists, empty)

No code changes are pending — `src/` is fully committed at `ff7e02f`. The planning docs above are the
only uncommitted work.

## 8. Key references / gotchas
- **Deploy command** and project/env details: project memory `project_intervals_mcp.md`, and the
  §"Deploy command" there. Deploys + secret changes + `git push` need explicit go-ahead per `CLAUDE.md`.
- **New tools inherit arg-widening automatically** (no per-param coercion) — `server.py` runs the
  widener after registration.
- **Reference patterns for the fixes:** `docs/connector-review-fixes.md` §"Reference patterns".
- **Full-transport tests:** `tests/test_arg_coercion.py` `_call()` via in-memory `Client(mcp)`.
