# Session Resume — intervals.icu MCP connector

**Last updated:** 2026-07-05 · **Repo:** `C:\Users\steph\intervals-icu-mcp` · **Branch:** `main`

Read this first to resume. It captures everything from the 2026-07-03→05 sessions: the shipped bug
fixes, the connector review, the hardening plan, the four resolved decisions, and the **in-flight R1
Google-OAuth setup** you were mid-way through.

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
| `Updates/01-critical-security.md` | 0 · Critical | R1 (Google-OAuth lock), R2 (gate destructive tools) | **In progress — see §5** |
| `Updates/02-stability-high.md` | 1 · High | R3 stream caps, R4 download guards, R5 route-path opt-out, R6 resilient list parsing | Ready |
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

1. [ ] Answer the §5 micro-decision (email + consent-screen type).
2. [ ] Create the Google OAuth client (Console) + store the two secrets (`gcloud`, above).
3. [ ] Fold the §5 prerequisites into `Updates/01-critical-security.md`; set `MCP_ALLOWED_EMAILS`.
4. [ ] Decide whether to **commit** the untracked planning docs (see §7).
5. [ ] Implement, in order: R1 → R2 (Phase 0), then Phases 1–4. One requirement per commit; keep the
       green gate. R1 pauses for: Google client creation (done in step 2), deploy approval, and the
       one-time Firestore token flush.

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
