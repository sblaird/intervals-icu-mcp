# Connector Hardening — Index & Reference

Reference map for the connector review fixes. **The implementable specs live in `Updates/`** — this
file is the overview, decisions log, traceability, and shared reference patterns. It is *not* itself a
task to implement.

**Baseline:** commit `ff7e02f`, revision `intervals-mcp-00018-bnm` · **Source:** 3-lens connector
review (stability / security / data coverage), 18 findings.

---

## Implementation order (files in `Updates/`)

| Phase | File | Requirements | Blocking decision? |
|-------|------|--------------|--------------------|
| 0 · Critical security | `Updates/01-critical-security.md` | R1, R2 | Resolved — ready (R1 needs a Google OAuth client + deploy) |
| 1 · High stability | `Updates/02-stability-high.md` | R3, R4, R5, R6 | No |
| 2 · Medium hardening | `Updates/03-medium-hardening.md` | R7, R8, R9, R10 | No |
| 3 · Polish | `Updates/04-polish.md` | R11, R12, R13 | No |
| 4 · Data enrichment | `Updates/05-data-enrichment.md` | R14, R15, R16 | No (R16 opt-in, D4) |

Work lowest phase first — all phases (0–4) are now unblocked. Each `Updates/` file is self-contained
(guardrails + requirements + acceptance criteria) and is moved to `Updates/Archive/` once implemented,
per `CLAUDE.md`.

---

## Decisions — RESOLVED 2026-07-04

| ID | Decision | Outcome |
|----|----------|---------|
| **D1** | Auth-gating approach | **Google-identity federation** — `authorize` federates to Google Sign-In; allowlist of one (`MCP_ALLOWED_EMAILS`, default Stephen's email). No password; no other credential accepted. |
| **D2** | Which writes to gate | **Deletes + `apply_sport_settings`** behind `ENABLE_WRITE_TOOLS` (default off). `create_*`/`update_*`/`mark_event_done`/`bulk_create`/`duplicate` stay available. |
| **D3** | API-key rotation | **No rotation.** Key was never the leaked credential. Instead, one-time **Firestore token-store flush** at R1 deploy to invalidate any token from the open window. |
| **D4** | Data-enrichment scope | **Ship everything** — R14/R15 fields + all five R16 tools. |

---

## Finding → requirement traceability

| Finding | Severity | Requirement | File |
|---------|----------|-------------|------|
| SEC-1 OAuth auto-approval | Critical | R1 | 01 |
| SEC-2 destructive tools exposed | High | R2 | 01 |
| STB-H1 unbounded streams | High | R3 | 02 |
| STB-H2 download base64 | High | R4 | 02 |
| STB-M6 route-similarity paths | Medium | R5 | 02 |
| STB-H3 atomic list parsing | High | R6 | 02 |
| STB-M4 required singleton fields | Medium | R6 | 02 |
| STB-M1 Firestore load trap | Medium | R7 | 03 |
| STB-M2 silent persist failure | Medium | R7 | 03 |
| STB-M5 Firestore LWW race | Medium | R7 | 03 |
| STB-M3 no retry/backoff | Medium | R8 | 03 |
| SEC-4 reflected error bodies | Medium | R9 | 03 |
| SEC-3 container runs as root | Medium | R10 | 03 |
| STB-L1 dual config pattern | Low | R11 | 04 |
| STB-L2 date parse crash | Low | R12 | 04 |
| STB-L4 unlogged handler exceptions | Low | R13 | 04 |
| STB-L3 httpx per call | Low | — note only, no action — | — |
| SEC-5 public metadata endpoints | Low | — accepted, no action — | — |
| Data: field/endpoint gaps | — | R14, R15, R16 | 05 |

---

## Global guardrails (restated in each `Updates/` file)

- Test-first; drive tool-path changes through the in-memory `Client(mcp)` (see `tests/test_arg_coercion.py`).
- `make can-release` must stay green: full suite, `ruff check`, `ruff format --check`, **pyright 0 errors** (strict `src/`).
- Reuse existing patterns; never introduce parallel ones.
- Conventional commits, one requirement per commit, reference the R-id.
- **No `gcloud run deploy`, key rotation, or `git push` without Stephen's go-ahead.**
- Don't rename existing tools/params (connector contract); add params as optional with defaults.

## Reference patterns (copy these, don't reinvent)

- **Resilient parse loop:** `client.py` `get_activities` (~L228–243) and `_build_streams_resilient`
  (~L41–70). R6's helper generalizes this.
- **Structured tool skeleton:** any tool in `tools/activity_analysis.py` — `ctx.get_state("config")`,
  `async with ICUClient(config)`, `ResponseBuilder.build_response`, `except ICUAPIError` then `except Exception`.
- **Full-transport test:** `tests/test_arg_coercion.py` `_call()` via in-memory `Client(mcp)`.
- **New-tool arg safety:** nothing extra needed — `server.py` runs `widen_tool_schemas_for_string_args(mcp)`
  after registration, so new int/array params already accept the stringified form claude.ai sends.
