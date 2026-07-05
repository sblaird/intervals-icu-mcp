# Update 03 — Medium Hardening (Phase 2)

**Source:** connector review, findings STB-M1, M2, M5, M3 and SEC-4, SEC-3. No blocking decisions.
Full context in `docs/connector-review-fixes.md`.

## Guardrails (apply to every requirement)
- Test-first. `make can-release` must stay green (full suite, ruff check+format, **pyright 0 errors**).
- Reuse existing patterns; `ResponseBuilder` for responses. Conventional commits, one per R-id.
- No `gcloud run deploy` / `git push` without Stephen's go-ahead.
- When all requirements are done, move this file to `Updates/Archive/`.

---

## R7 — Firestore OAuth resilience  `MEDIUM`  `STB-M1` + `STB-M2` + `STB-M5`

**Problem.** On a load exception the code sets `self._loaded = True` and never retries — one Firestore
blip after cold start strands the instance with empty auth (every call 401s until next cold start)
while Firestore holds valid tokens. `_persist` swallows write failures silently. Full-document `set()`
is last-writer-wins under concurrent mutation.

**Files.** `src/intervals_icu_mcp/firestore_oauth.py` (~L138–146).

**Implementation.**
- Only set `_loaded = True` on a **successful** load; on failure leave it false so the next request
  retries (add a short backoff if a tight loop is a risk).
- On `_persist` failure, log at `error` and surface a signal — a counter, or re-raise on token-issuing
  paths (`exchange_authorization_code`) so the client sees the failure rather than a false "Connected".
- (M5, low probability under `max-instances=1`) prefer a Firestore transaction / etag-checked write or
  field-level merge over a blind whole-doc `set()`; implement if cheap, otherwise document as accepted risk.

**Acceptance criteria.**
- [ ] Test: first load raises → `_loaded` stays false → a second load succeeds and populates tokens.
- [ ] Test: `_persist` failure is not silently swallowed (asserts log/raise per chosen approach).
- [ ] Existing `test_firestore_oauth.py` suite still passes.

---

## R8 — Retry/backoff for transient upstream failures  `MEDIUM`  `STB-M3`

**Problem.** `_request` is single-shot: 429, 5xx and connect errors surface immediately as tool
failures; the LLM may re-issue the whole chain.

**Files.** `client.py` (`_request`, ~L124–177).

**Implementation.** 1–2 retries with jittered backoff for 429 / 502 / 503 / 504 and
`httpx.RequestError`; honour `Retry-After` on 429; stay within the 30 s client timeout; do **not**
retry other 4xx or successes. Keep the existing structured logging.

**Acceptance criteria.**
- [ ] Test: mocked 503-then-200 succeeds after retry.
- [ ] Test: persistent 500 returns a structured `api_error` after the retry budget.
- [ ] Test: 404 is not retried.

---

## R9 — Don't reflect upstream error bodies to the caller  `MEDIUM`  `SEC-4`

**Problem.** Up to 500 chars of intervals.icu's response body are placed into the `ICUAPIError` message
returned to the model.

**Files.** `client.py` (~L163–174).

**Implementation.** Keep the existing `logger` line with the body snippet; return a status-appropriate
generic message (e.g. "intervals.icu returned 404 for this request") from `ICUAPIError`.

**Acceptance criteria.**
- [ ] Test: an error response body is not present in the returned tool payload but is logged.

---

## R10 — Run the container as non-root  `MEDIUM`  `SEC-3`

**Files.** `Dockerfile` (final stage).

**Implementation.** Add a non-root user in the final stage: `RUN useradd -m app && chown -R app /app`
then `USER app` (adjust to the actual app dir). Verify uv/entrypoint work under the non-root user.

**Acceptance criteria.**
- [ ] The built image's runtime user is non-root.
- [ ] `make docker/build && make docker/run` starts successfully.
