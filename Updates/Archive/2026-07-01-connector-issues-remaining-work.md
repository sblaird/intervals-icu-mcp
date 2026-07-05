# Spec: Complete the 2026-07-01 connector issues remediation

**Author:** handoff from coaching-session issues log (`intervals_icu_mcp_issues_2026-07-01.md`)
**Target executor:** Claude Code CLI
**Repo:** `sblaird/intervals-icu-mcp` (fork of `eddmann/intervals-icu-mcp`)
**Deployment:** self-hosted remote MCP server on Cloud Run, OAuth Custom Connector for claude.ai
(`src/intervals_icu_mcp/remote_server.py`, streamable-http, FastMCP 2.12.4)

---

## 0. Background & what is ALREADY done (do not redo)

Two of the six reported issues were fixed and shipped to `main` on 2026-07-01
(commits `cf293d5`, `06204e4`):

- **Issue #3 — malformed `latlng` crashing the whole streams response.** Fixed.
  - `src/intervals_icu_mcp/models.py`: `ActivityStreams._reshape_flat_latlng`
    reshapes a flat `[lat, lng, lat, lng, …]` list into pairs.
  - `src/intervals_icu_mcp/client.py`: `_build_streams_resilient()` drops only
    the offending stream instead of discarding the entire response.
  - Tests: `tests/test_streams_and_events.py::TestActivityStreamsResilience`.
- **Issue #4 (partial) — `create_event` WORKOUT with no type.** Fixed the raw
  HTTP 422: `create_event` now fails fast with a clear `event_type is required`
  message (`src/intervals_icu_mcp/tools/event_management.py`).
  - Tests: `tests/test_streams_and_events.py::TestCreateEventValidation`.

This spec covers the **remaining** work: issues #1, #2, #4 (external doc + optional
alias), #5, plus repo-health items (CI/pyright) and one enhancement.

### Local verification commands (CI-equivalent — run these for every task)

```bash
uv run ruff check        # must stay "All checks passed!"
uv run ruff format --check
uv run pytest            # must stay green (currently 97 passing)
uv run pyright           # see Task E — 39 PRE-EXISTING errors baseline; do not regress
```

> **CI note:** `.github/workflows/release.yml` runs on push to `main` and calls
> `test.yml` (ruff → pyright → pytest). **Actions has never run on this fork**
> (`gh run list` → empty). GitHub gates Actions on forks behind a one-time manual
> "I understand my workflows, enable them" click in the repo's **Actions** tab.
> Until that is clicked, no CI runs — see Task E.

---

## Task A — Issues #1 & #2: integer / array params rejected as strings (HIGH)

### Symptom (verbatim from the log)
- `get_recent_activities(days_back=1, limit=3)` → `Input validation error: '3' is not of type 'integer'`
- `get_calendar_events(days_ahead=14)` → `Input validation error: '14' is not of type 'integer'`
- `get_wellness_data(days_back=3)` → `'3' is not of type 'integer'`
- `get_activity_streams(streams=["watts","heartrate"])` →
  `Input validation error: '["watts","heartrate","cadence","time"]' is not valid under any of the given schemas`

Workaround observed: omitting the parameter (using the default) always worked.
The failure was intermittent and sometimes cleared by a tool reload.

### Hypothesis (CONFIRM before coding — do not assume)
The values are arriving at the server **as JSON strings** (`"3"`, `'["watts",…]'`)
rather than native types, and FastMCP/pydantic validation rejects them against the
declared `int` / `list[str]` schema. The single-quoted values in the error text and
the "not valid under any of the given schemas" wording are the tell.

Because this server is **self-hosted** (the boundary is claude.ai → HTTP JSON-RPC →
this FastMCP app), a server-side coercion fix is viable and deployable. This is the
key difference from a stock connector — do not dismiss this as "client-side only."

### Step 1 — Reproduce and capture the real payload (REQUIRED first)
Do not write a fix until you have proven what the wire payload looks like.
- Add temporary debug logging (or a FastMCP middleware `on_call_tool` hook) that
  logs the raw `arguments` dict and each value's Python `type()` for one affected
  tool, deploy/run locally, and drive a call with an integer arg.
- Alternatively, write a test that invokes the tool layer with string args
  (`days_back="3"`) and confirm it reproduces the validation rejection locally via
  the FastMCP tool-call path (not by calling the plain Python function, which
  bypasses schema validation).
- Record findings in the PR description. If the values arrive as native ints, the
  hypothesis is wrong — stop and escalate (it is then a claude.ai connector bug,
  report upstream, do not hack the schema).

### Step 2 — Implement server-side coercion (only if Step 1 confirms strings)
Preferred approach, in priority order — pick the one that fits FastMCP 2.12.4:

1. **A FastMCP middleware that coerces arguments before validation.**
   There is already a middleware seam: `src/intervals_icu_mcp/middleware.py`
   (`ConfigMiddleware`, registered in `server.py:17`). Add a
   `CoerceScalarArgsMiddleware` that, in the tool-call hook, walks the incoming
   arguments and, **guided by each tool's declared JSON schema**, coerces:
   - JSON-string integers → `int` (only when the target type is integer/number),
   - JSON-string arrays (`'["a","b"]'`) → `list` (only when target is array),
   - leaving already-correct types untouched.
   - **Critical:** confirm middleware runs *before* pydantic argument validation in
     FastMCP 2.12.4. If it does not, this approach won't work — fall back to (2).
2. **Per-parameter permissive typing with a coercing validator.** If middleware
   can't intercept pre-validation, widen the hot-path params to accept
   `int | str` / `list[str] | str` and coerce inside the tool (or via a shared
   `Annotated` type with a `BeforeValidator`). Apply only to the params named in
   the log first (`days_back`, `days_ahead`, `limit`, `streams`), not every param.

Whichever path: **do not change the advertised schema in a way that loses the
integer/array type hint** for well-behaved clients. Coercion should be additive.

### Acceptance criteria
- New tests prove that a tool call with `days_back="3"` (string) and
  `streams='["watts","heartrate"]'` (JSON string) succeed and behave identically
  to the native-typed call.
- Native-typed calls (`days_back=3`, `streams=["watts"]`) still pass unchanged.
- `uv run pytest`, `ruff`, and `pyright` (no new errors) all pass.
- PR description documents the Step-1 payload evidence.

---

## Task B — Issue #5: OAuth "No approval received" / reconnect-to-fix (HIGH)

### Symptom
Write calls (and once a **read**, `get_wellness_data(days_back=365)`) returned
`No approval received` repeatedly; **disconnecting and reconnecting the connector
(fresh token) reliably fixed it.**

### Root cause (high confidence — server-side deployment issue)
`remote_server.py` documents this exact failure mode: with the default in-memory
OAuth token store (`OAUTH_TOKEN_STORE=memory`) and/or stateful HTTP, **Cloud Run
cold starts / new revisions wipe OAuth + session state**, stranding claude.ai until
it re-authenticates. The "reconnect fixes it" evidence matches token/state loss,
not an approval-UI problem.

### Work
1. Verify the deployed Cloud Run service sets:
   - `OAUTH_TOKEN_STORE=firestore` (so OAuth state survives revisions —
     `FirestoreOAuthProvider` already exists in `firestore_oauth.py`), and
   - `MCP_STATELESS_HTTP=1` (default; every request gets a fresh transport so a
     cold start cannot strand the client).
   Document current values and correct them if they are `memory` / stateless off.
2. Confirm `firestore_oauth.py` persistence actually round-trips (there are
   pyright errors in this file — see Task E; verify they are not masking a real
   bug in token read/write).
3. Investigate the anomalous **read** requiring approval: determine whether an
   expired/necessary token refresh is surfaced to claude.ai as an approval prompt.
   If token refresh is the trigger, ensure refresh is handled server-side.

### Acceptance criteria
- Documented: the production env-var values before/after.
- A deploy (or a documented manual test) showing OAuth state survives a new
  revision / cold start without a client reconnect.
- Note: this task is largely deploy/config + verification; code changes only if
  `firestore_oauth.py` is found to mis-persist.

---

## Task C — Issue #4 remainder: `type` vs `event_type` doc mismatch (MEDIUM)

The repo tool is already correct (`event_type`, mapped to the API's `type`). Two
remaining items:

1. **External (cannot be done from the repo):** the claude.ai **project
   instructions, "Section 12 / EVENT FIELDS"** document the parameter as `type`,
   which makes Claude call `create_event(type="Ride")` and hit
   `Unexpected keyword argument [type]`. Update that project instruction text to say
   `event_type`. *(Flagged here for the human — Claude Code CLI cannot edit
   claude.ai project settings.)*
2. **Optional in-repo hardening:** make `create_event` tolerate a `type` alias so
   following the old docs still works. If implemented, do it without shadowing the
   builtin `type` in a way that trips ruff (A002) — e.g. accept an alias via schema
   metadata rather than a bare `type` parameter, and map it to `event_type`.
   `bulk_create_events` already accepts `type` directly (confirmed in the log).

### Acceptance criteria
- If the alias is implemented: a test proving `create_event` accepts the alias and
  maps it to the API `type` field; existing tests still green.
- If skipped: leave a short note in the PR that (1) above is the real fix.

---

## Task D — Enhancement: surface dropped/partial streams (LOW)

Follow-on from the shipped Issue #3 fix. `_build_streams_resilient` currently drops
malformed streams silently (only a `logger.warning`). The tool response does not
tell the LLM a stream was omitted.

### Work
- In `src/intervals_icu_mcp/tools/activity_analysis.py::get_activity_streams`, add
  a `dropped_streams: list[str]` (and/or `partial: bool`) field to the response
  `metadata` when the resilient builder discarded any requested stream.
- Thread the dropped-stream names out of `client._build_streams_resilient`
  (e.g. return them alongside the model, or expose via a small result object) so
  the tool can report them. Keep the happy path unchanged.

### Acceptance criteria
- Test: a streams payload with one unparseable stream returns the good streams AND
  lists the dropped one in metadata.
- Existing streams tests still green.

---

## Task E — Repo health: enable CI and fix the pyright baseline (MEDIUM)

### Current state (measured 2026-07-01)
- `uv run ruff check` → clean. `uv run pytest` → 97 passing.
- `uv run pyright` → **39 pre-existing errors** (baseline before this remediation;
  the shipped fixes added zero net errors). If Actions is enabled, the pyright CI
  step will FAIL until these are addressed. Breakdown of the pre-existing errors:
  - `src/intervals_icu_mcp/remote_server.py` — several. Notably
    `"ClientRegistrationOptions"`/`"RevocationOptions" is not exported from
    module "fastmcp.server.auth.auth"` and related arg-type errors. This is
    **FastMCP API drift** (pinned `fastmcp>=2.12.4`); the import path likely moved.
    Fix the imports to the current FastMCP location and re-verify the OAuth wiring.
  - `src/intervals_icu_mcp/firestore_oauth.py` — several `reportUnknownVariableType`
    (untyped Firestore data). See Task B — confirm none hide a real persistence bug.
  - `src/intervals_icu_mcp/tools/activities.py:824` — `sort(key=…)` returns
    `datetime | None`, which isn't `SupportsRichComparison`. Give the key a total
    order (e.g. sort `None` last with a sentinel) — this is a latent runtime bug if
    any activity has a null date.

### Work
1. Enable GitHub Actions on the fork (human step: Actions tab → enable workflows).
   Document it in the PR; Claude Code CLI cannot click this.
2. Fix the pyright errors, starting with `remote_server.py` (API drift — highest
   risk of a real breakage) and `activities.py:824` (latent sort crash).
3. Decide policy for the remaining `firestore_oauth.py` unknown-type warnings:
   either type them or scope pyright appropriately — but do not silence errors that
   mask real bugs.

### Acceptance criteria
- `uv run pyright` error count strictly decreases; target 0 for
  `remote_server.py` and `activities.py`.
- CI (once enabled) passes ruff + pyright + pytest on `main`.

---

## Suggested execution order
1. **Task E** partial (fix `remote_server.py` import drift + `activities.py` sort)
   — unblocks CI and de-risks the OAuth path used by Task B.
2. **Task B** (OAuth/session persistence) — highest user-facing reliability win.
3. **Task A** (arg coercion) — highest frequency, but gate on the Step-1 payload
   evidence.
4. **Task C** / **Task D** — smaller; do alongside.

## Guardrails for the executor
- Do NOT force-push or use `--no-verify`.
- Do NOT commit secrets (`.env`, `*.key`, `*.pem`).
- Keep changes minimal and matched to the existing code style (ruff, 100-col).
- For each task, add tests first where practical, then implement.
- If Task A Step 1 disproves the string-serialization hypothesis, STOP and report —
  do not widen schemas speculatively.
- When each task is complete and verified, move this spec to `Updates/Archive/`
  per the project's Spec Updates Workflow.
