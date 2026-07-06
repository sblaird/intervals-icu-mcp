# Update 04 — Low-Severity Polish (Phase 3)

**Source:** connector review, findings STB-L1, L2, L4. No blocking decisions. (STB-L3 — new httpx
client per call — is note-only, **no action**.) Full context in `docs/connector-review-fixes.md`.

## Guardrails (apply to every requirement)
- Test-first. `make can-release` must stay green (full suite, ruff check+format, **pyright 0 errors**).
- Reuse existing patterns; `ResponseBuilder` for responses. Conventional commits, one per R-id.
- When all requirements are done, move this file to `Updates/Archive/`.

---

## R11 — Unify config loading  `LOW`  `STB-L1`

**Problem.** `gear.py` and `sport_settings.py` (11 sites) use `load_config()` + bare-string errors
instead of the middleware-injected `ctx.get_state("config")` used by 45 other tools; error shape
differs and `.env` is re-read per call.

**Files.** `tools/gear.py` (L20,134,204,286,330,401), `tools/sport_settings.py`
(L112,176,242,291,351), `middleware.py` (L38–39).

**Implementation.** Migrate these tools to `config = ctx.get_state("config")` and
`ResponseBuilder.build_error_response` for the not-configured case, matching the other tools. Update the
sport-settings tests that currently monkeypatch `load_config`/`validate_credentials` accordingly.

**Acceptance criteria.**
- [ ] No tool returns a bare error string; all use structured errors.
- [ ] Sport-settings + gear tests updated and green.

---

## R12 — Harden `format_date_with_day`  `LOW`  `STB-L2`

**Files.** `src/intervals_icu_mcp/response_builder.py` (~L47).

**Implementation.** Wrap `datetime.fromisoformat` in try/except; on failure return the raw string.

**Acceptance criteria.**
- [ ] Test: a garbage date string returns the raw value, not an exception.

---

## R13 — Log exceptions in tool handlers  `LOW`  `STB-L4`

**Problem.** The blanket `except Exception` in ~59 handlers builds an error response without logging the
traceback (why the latlng bug was opaque).

**Implementation.** Add `logger.exception(...)` in the `except Exception` branch of tool handlers —
prefer a shared decorator/helper over touching 59 sites by hand. Do not change the returned error shape.

**Acceptance criteria.**
- [ ] An induced handler exception is logged with traceback and still returns a structured error.
