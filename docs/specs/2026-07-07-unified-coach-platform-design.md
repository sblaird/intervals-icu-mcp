# Unified AI Coach Platform — Design & Requirements

**Date:** 2026-07-07 · **Status:** Approved (pending mockup review) · **Owner:** Stephen Laird

## 1. Vision

Move the conversational coaching surface OFF claude.ai Projects onto owned infrastructure,
reusing every existing asset. claude.ai's chat cannot invoke the Custom Connector's tools
(see `docs/SESSION-RESUME.md`, 2026-07-07 entries) despite a provably healthy server. Rather
than keep fighting a platform-side surfacing bug, the chat moves to GravelFit — where we
control the model, the tool wiring, and the UX — while intervals.icu remains the canonical
calendar/workout/analytics display.

**One sentence:** *GravelFit becomes a chat-first AI coach powered by the intervals-icu MCP
server via the Anthropic API's MCP connector, with intervals.icu as the display surface for
everything visual.*

## 2. Architecture

```
┌─────────────────┐   HTTPS/SSE    ┌──────────────────────┐
│ GravelFit SPA   │ ─────────────▶ │ GravelFit backend    │
│ React/Vite      │                │ FastAPI · Fly.io     │
│ Vercel          │ ◀───────────── │ (gravelfit-backend)  │
└───────┬─────────┘   stream        └─────────┬────────────┘
        │ deep links                          │ Messages API (stream)
        ▼                                     │ betas: mcp-client-2025-11-20
┌─────────────────┐                           ▼
│ intervals.icu   │                ┌──────────────────────┐
│ calendar/fitness│                │ Anthropic API        │
│ activity pages  │                │ (server-side MCP     │
└─────────────────┘                │  tool loop)          │
        ▲                          └─────────┬────────────┘
        │ REST (Basic Auth)                  │ Authorization: Bearer MCP_SERVICE_TOKEN
        │ (Today panel only)                 ▼
┌───────┴─────────┐   REST        ┌──────────────────────┐
│ GravelFit       │ ◀──────────── │ intervals-icu-mcp    │
│ intervals_client│               │ Cloud Run · LEAN=16  │
└─────────────────┘               └─────────┬────────────┘
                                            │ intervals.icu API
                                            ▼
                                   intervals.icu (data source of truth)
```

Roles:

| Asset | Role in new iteration |
|---|---|
| **intervals-icu-mcp** (Cloud Run) | Single AI data/tool layer. `LEAN_TOOLS=1` → 16 coaching-core tools. Google OAuth kept for claude.ai fallback; new static service token for server-to-server. |
| **GravelFit frontend** (Vercel) | Chat-first companion: unified coach chat + Today panel + deep links. Everything else removed. |
| **GravelFit backend** (Fly.io) | Chat orchestration via Messages API MCP connector; thin `/api/today` endpoint from a retained direct intervals.icu client; Turso/libsql persistence. |
| **intervals.icu** | Canonical display: calendar, workout detail, fitness charts, activity analysis. Reached by deep link, never rebuilt in-app. |
| **claude.ai connector** | Retired as primary surface; OAuth flow kept alive as a free fallback (mobile Claude app, future fix). |

## 3. Functional requirements

- **FR1 — Unified chat.** One streaming coach conversation backed by the Messages API MCP
  connector (`mcp_servers` + `mcp_toolset`, beta `mcp-client-2025-11-20`; empirically confirm
  shape before coding, fallback `mcp-client-2025-04-04`). Merged fitness-coach + nutrition
  persona in one byte-stable system prompt with `cache_control: ephemeral`. The assistant:
  - uses the 16 lean tools for all training data;
  - emits intervals.icu deep links when referencing activities (`https://intervals.icu/activities/{id}`),
    the calendar (`https://intervals.icu/calendar`), or fitness (`https://intervals.icu/fitness`);
  - **confirms with the athlete before any calendar write** (create/update/bulk/mark-done) —
    there is no claude.ai approval UI anymore, so the prompt enforces a propose→confirm→execute loop;
  - handles `pause_turn` by resuming the turn (max ~5 continuations).
- **FR2 — Today panel.** `GET /api/today` returns readiness (HRV, resting HR, sleep),
  CTL/ATL/TSB with TSB interpretation, and the next planned workout — served by the retained
  thin `intervals_client.py` with existing cache TTLs. Never routed through Claude (speed + cost).
- **FR3 — History.** Existing `coach_chats` single-thread persistence and `/history` endpoints
  stay; last-20 replay. Conversation IDs deferred.
- **FR4 — Service auth.** MCP server accepts a static bearer token (`MCP_SERVICE_TOKEN`,
  ≥32 chars, `secrets.compare_digest`) via a `load_access_token` override on `GoogleGateProvider`
  (`src/intervals_icu_mcp/google_oauth.py:222`), falling through to the normal OAuth store.
  Fail-closed when unset. Token lives in GCP Secret Manager (Cloud Run) and Fly secrets.
- **FR5 — intervals.icu as display.** No in-app calendar, planning, or analytics rebuild.
  Deep links only, `target="_blank"`.

## 4. Non-functional requirements

- **Cost/tokens:** 16 lean tool schemas (~3–6K tokens) vs 55 (~15–25K) per request; byte-stable
  system prompt + stable tool list keep the prompt cache warm (`cache_read_input_tokens > 0`
  on consecutive turns). "Today is YYYY-MM-DD" injected into the user turn, not the system prompt.
- **Model:** `claude-opus-4-8` (confirmed) — chosen for coaching judgment; cost is negligible at
  single-user volume, so quality drives the pick. `claude-sonnet-5` is the fallback if end-to-end
  latency (MCP tool loops + Cloud Run cold start + Opus generation) proves annoying; the model is a
  `config.py` value so it can be A/B'd with one env change. Note: "fast mode" is a Claude Code CLI
  feature, not a Messages API parameter — it does not apply to the coach runtime. The current prod
  model `claude-sonnet-4-20250514` is **retired** — migration is mandatory.
- **Latency:** Cloud Run cold start can delay Claude's first tool call by seconds. Accept
  initially; `min-instances=1` (~$5–10/mo) if it annoys. `MCP_STATELESS_HTTP=1` already
  protects correctness across cold starts.
- **Security:** destructive tools (5 deletes + `apply_sport_settings`) remain excluded from
  the lean set regardless of flags. Service token rotation: regenerate, set in Secret Manager
  + Fly, redeploy both.
- **Single user.** No multi-tenant concerns; the token + OAuth allowlist model is sufficient.

## 5. Phased roadmap

### Phase 0 — MCP repo finishing (intervals-icu-mcp)
1. Commit staged `LEAN_TOOLS` work (`server.py` + `tests/test_lean_tool_set.py`).
2. Add service-token override on `GoogleGateProvider`; new `tests/test_service_token.py`
   (correct / wrong / unset / short token × both provider variants).
3. Deploy Cloud Run with `LEAN_TOOLS=1` + `MCP_SERVICE_TOKEN` (Secret Manager). Use the
   `--update-env-vars` MERGE form only (see SESSION-RESUME deploy trap).
4. **Gate:** unauthenticated → 401; `tools/list` with bearer → exactly 16; claude.ai OAuth
   connector regression passes.

### Phase 1 — Backend unified chat (gravelfit/backend)
1. **Curl gate:** prove Messages API + MCP connector against live Cloud Run; pin exact stream
   block types (`mcp_tool_use`/`mcp_tool_result`) and tool naming.
2. Bump `anthropic` SDK; add `mcp_server_url` / `mcp_service_token` to `config.py`.
3. Merged `coach_system_prompt.md` (coach + nutrition + tool guidance + deep-link + confirm-before-write rules).
4. New `services/unified_coach_ai.py` — no client-side tool loop; maps stream → existing SSE
   protocol (`text|tool_call|tool_result|done`) so `FitnessCoach.jsx` needs zero changes.
5. Rewire `routers/coach.py`; persistence untouched. Ship MCP-only (no local DB tools initially).
6. Fly secrets + deploy. **Gate:** streamed chat calls `get_fitness_summary`; a scheduling
   request creates a real intervals.icu event after in-chat confirmation; cache hits on turn 2.

### Phase 2 — Frontend chat-first (gravelfit/frontend)
1. `src/pages/Coach.jsx` (reuses `FitnessCoach.jsx`) + `src/components/today/TodayPanel.jsx`;
   backend `GET /api/today`.
2. `App.jsx`: `/` → Coach; legacy routes `Navigate` to `/`; old pages left unrouted.
   `Layout.jsx` nav collapses to Coach + external intervals.icu links; remove double-mounted chat.
3. **Gate:** Vercel preview streams on mobile; Today panel <1s cached; deep links land correctly.

### Phase 3 — Cleanup
- Retire unused gravelfit routers/services and the old `TOOLS`/`execute_tool()` machinery;
  slim `intervals_client.py` to Today-panel needs; delete unrouted pages/components.
- Keep the MCP server's Google OAuth flow (free, coexists with service token).
- README architecture section + secret-rotation runbook. Update SESSION-RESUME.

## 6. Risks

| Risk | Mitigation |
|---|---|
| MCP connector beta shape drift | Phase 1 curl gate before any code; build to whichever shape the API accepts. |
| Streaming block names differ from assumption | Pinned in the same curl gate before writing the SSE mapper. |
| Calendar writes without platform approval UI | System-prompt propose→confirm→execute rule; deletes structurally excluded. |
| Cloud Run cold start mid-generation | Accept, or `min-instances=1`. |
| Prod chat already broken (retired model) | One-line hotfix available in `fitness_coach_ai.py` if needed before Phase 1. |

## 7. End-to-end verification

1. Phone → GravelFit URL → "How's my training load? Plan next week." → tool chips fire →
   plan proposed → confirm in chat → events appear on the intervals.icu calendar.
2. Today panel numbers match intervals.icu/fitness.
3. Cloud Run logs show `anthropic-mcp-connector` client id, no 401s; claude.ai connector
   still authenticates via Google OAuth as fallback.
