# Update 01 — Critical Security (Phase 0)

> **Decisions resolved (2026-07-04) — ready to implement.** Operational notes:
> - **R1 needs a Google OAuth client** created in the `intervals-mcp-2026` GCP project, plus new
>   secrets and a redeploy — do the code first, then pause for Stephen to create the client / confirm
>   the deploy (per `CLAUDE.md`, deploys and secret changes need his go-ahead).
> - **Start R1 with the short spike** (confirm claude.ai's connector does DCR and opens `authorize`
>   in the browser — it appears to). Report findings in the PR, then implement.
> - **At R1 deploy:** flush the Firestore token store once (see R1 post-deploy). No API-key rotation.

**Source:** connector review, findings SEC-1 (Critical) & SEC-2 (High). Full context and traceability
in `docs/connector-review-fixes.md`.

## R1 prerequisites — Stephen, before the R1 deploy (Console tasks)

- [x] **Micro-decision (RESOLVED 2026-07-06):** workspace `stephen@bramblepathdigital.com` +
      **Internal** consent screen. → `MCP_ALLOWED_EMAILS=stephen@bramblepathdigital.com`
- [ ] **OAuth consent screen** (project `intervals-mcp-2026`): APIs & Services → OAuth consent screen →
      Internal (or External + test user) → name "intervals-mcp connector" → save. Scopes: `openid` +
      `email` (defaults, nothing to add).
- [ ] **Create the Web client:** APIs & Services → Credentials → Create credentials → OAuth client ID →
      Web application. Authorized redirect URI:
      `https://intervals-mcp-840283109221.us-central1.run.app/auth/google/callback`
- [ ] **Store the secrets yourself** (keeps them out of chat):
      ```bash
      printf '%s' 'PASTE_CLIENT_ID'     | gcloud secrets create GOOGLE_OAUTH_CLIENT_ID     --data-file=- --project=intervals-mcp-2026
      printf '%s' 'PASTE_CLIENT_SECRET' | gcloud secrets create GOOGLE_OAUTH_CLIENT_SECRET --data-file=- --project=intervals-mcp-2026
      ```
- [ ] Report "secrets created" + the chosen email → assistant adds the new `--set-secrets` entries and
      `MCP_ALLOWED_EMAILS=<chosen email>` to the deploy command. (`gcloud auth login` may need a refresh.)

## R1 deploy checklist (after code + prerequisites)

- [ ] Deploy with the two new secrets + `MCP_ALLOWED_EMAILS` (needs Stephen's go-ahead).
- [ ] **D3 one-time token flush:** clear the Firestore `oauth_state/singleton` doc so tokens issued
      during the open window die, then re-authorize the connector once as Stephen.
- [ ] Verify: unattended `register → authorize` redirects to Google; claude.ai re-authorization works.

## Guardrails (apply to both requirements)
- Test-first. Drive tool-path changes through the in-memory `Client(mcp)` (see `tests/test_arg_coercion.py`).
- `make can-release` must stay green: full suite, `ruff check`, `ruff format --check`, **pyright 0 errors** (strict `src/`).
- Reuse existing patterns — the `.well-known` custom routes in `remote_server.py` are the model for R1's callback route; `authlib` (already a dependency) handles the Google OAuth client. Secrets come from Secret Manager and are never logged.
- Conventional commits, one requirement per commit, reference the R-id.
- **No `gcloud run deploy`, secret creation, or `git push` without Stephen's go-ahead.**
- Don't rename existing tools/params (connector contract); add params as optional.
- When both requirements are done, move this file to `Updates/Archive/`.

---

## R1 — Lock the OAuth flow to Stephen's Google identity  `CRITICAL`  `SEC-1`

**Decision D1:** the MCP server stays its own OAuth Authorization Server to claude.ai, but the
**user-authentication step federates to Google Sign-In and allowlists exactly one email**. No shared
password; no other credential is accepted.

**Problem.** The server uses fastmcp's `InMemoryOAuthProvider` (via `FirestoreOAuthProvider`), a testing
stub. DCR is open and `authorize()` issues a code with **zero user consent** — no identity check at all.
Anyone who knows the URL can run `/register → /authorize → /token` headless and obtain a token that runs
every tool against the single intervals.icu key.

**Files.** `src/intervals_icu_mcp/remote_server.py` (provider construction ~L59–83; custom-route pattern
for the callback), `src/intervals_icu_mcp/firestore_oauth.py` (override `authorize`), fastmcp's
`InMemoryOAuthProvider`.

**Step 1 — spike (do first).** Confirm claude.ai's Custom Connector OAuth flow: DCR support, and that it
opens the `authorize` URL in the user's browser (so a mid-flow bounce to Google works). Record findings
in the PR; implement to that reality.

**Step 2 — implement (Google-identity federation).**
- Register a **Google OAuth 2.0 Web client** in project `intervals-mcp-2026`. Store
  `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` in Secret Manager. Authorized redirect URI:
  `https://intervals-mcp-840283109221.us-central1.run.app/auth/google/callback`.
  *(Creating the Google client is a manual GCP step — pause for Stephen.)*
- Override `authorize()` in `FirestoreOAuthProvider` (or a thin subclass) so that instead of
  auto-issuing a code it:
  1. Stashes the pending claude.ai authorization request (client_id, redirect_uri, state, PKCE
     `code_challenge`, scopes) keyed by a short-lived random nonce.
  2. Redirects the browser to Google's authorization endpoint with that nonce as `state`
     (scope `openid email`).
- Add a custom route **`/auth/google/callback`** (mirror the `.well-known` routes) that:
  1. Exchanges the Google code for tokens and verifies the `id_token` (use `authlib`).
  2. Extracts the verified `email` + `email_verified`; checks `email` against **`MCP_ALLOWED_EMAILS`**
     (comma-separated env allowlist, default = Stephen's address). Reject with **401** and issue nothing
     if not allowlisted or email unverified.
  3. On success, retrieves the stashed request by nonce and completes the original authorize — issue the
     MCP auth code and redirect to claude.ai's `redirect_uri` with `code` + original `state`.
- Preserve the base provider's PKCE and redirect-uri validation. Everything downstream (`/token`
  exchange, `/mcp` tool calls with the bearer) is unchanged — it's protected because a bearer can only
  be obtained by passing the Google gate.

**New config.** Secrets `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`; env
`MCP_ALLOWED_EMAILS` (default Stephen's email). Add all three to the deploy command
(`--set-secrets` / `--set-env-vars`).

**Acceptance criteria.**
- [ ] An unattended `register → authorize → token` that does **not** complete Google sign-in as an
      allowlisted user **fails to obtain a token** (test asserts the callback rejects and issues no code).
- [ ] Signing in via Google as an allowlisted email completes the flow; the claude.ai connector can be
      (re)authorized end-to-end.
- [ ] A **non-allowlisted** Google account is rejected with 401 and gets no code (test).
- [ ] An unverified email is rejected (test).
- [ ] Google client secret + intervals.icu key are sourced from Secret Manager, never logged/echoed.
- [ ] Tests mock the Google token exchange / id_token verification; cover allowlisted, non-allowlisted,
      unverified, and missing-token cases.

**Post-deploy (D3 — one-time, replaces API-key rotation).** After R1 is deployed, **flush the Firestore
OAuth token store once** (clear the `oauth_state/singleton` doc) so any token issued during the open
window is invalidated, then re-authorize the connector once as Stephen. No API-key rotation. Document
this as a deploy checklist item; do not run it without Stephen.

---

## R2 — Gate destructive tools behind a flag  `HIGH`  `SEC-2`

**Decision D2:** gate the five delete tools **plus `apply_sport_settings`** (it recomputes training load
and zones across all history — broad, annoying to reverse). Keep the everyday `create_*` / `update_*` /
`mark_event_done` / `bulk_create_events` / `duplicate_event` tools available — they're low blast-radius
and used from claude.ai. With R1 locking access to Stephen only, R2 is defense-in-depth against
prompt-injection during a legitimate session, so gating the irreversible actions is the right scope.

**Files.** `src/intervals_icu_mcp/server.py` (tool registration block ~L78–153).

**Gated set (registered only when `ENABLE_WRITE_TOOLS` is true):**
`delete_activity` · `delete_event` · `bulk_delete_events` · `delete_gear` · `delete_sport_settings` ·
`apply_sport_settings`

**Implementation.**
- Add env flag `ENABLE_WRITE_TOOLS` (default `false`). Wrap the registration of the six gated tools in a
  check like `os.getenv("ENABLE_WRITE_TOOLS","").lower() in {"1","true","yes"}`.
- All other write tools (`update_*`, `create_*`, `bulk_create_events`, `duplicate_event`,
  `mark_event_done`) remain registered unconditionally.
- Log at startup which tool set is active (mirror the widen-count startup log).
- Hosted deploy leaves the flag unset (off). Flip it on locally/stdio when a delete or a settings-apply
  is genuinely needed.

**Acceptance criteria.**
- [ ] With the flag unset, `mcp.get_tools()` contains none of the six gated tools; calling one returns
      not-found, not an execution.
- [ ] With `ENABLE_WRITE_TOOLS=true`, all six register and function as before.
- [ ] `create_*` / `update_*` / `mark_event_done` are present regardless of the flag.
- [ ] Test asserts presence/absence of the gated set under both flag states.
