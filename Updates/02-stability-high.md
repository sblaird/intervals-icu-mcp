# Update 02 — High-Severity Stability (Phase 1)

**Source:** connector review, findings STB-H1, H2, H3, M4, M6. No blocking decisions — ready to
implement. Full context in `docs/connector-review-fixes.md`.

These are the failure modes that bite in normal use: the payload issues are what forced the switch to
Strava for second-by-second data, and the strict-parsing issue is the identical drift class that
caused both the latlng and SportSettings bugs.

## Guardrails (apply to every requirement)
- Test-first. Drive tool-path changes through the in-memory `Client(mcp)` (see `tests/test_arg_coercion.py`).
- `make can-release` must stay green: full suite, `ruff check`, `ruff format --check`, **pyright 0 errors** (strict `src/`).
- Reuse existing patterns — `ResponseBuilder` for responses; resilient parsing per `client.py`
  `_build_streams_resilient` (~L41–70) and the per-item loop in `get_activities` (~L228–243); new
  int/array params inherit the global arg-widening (no per-param coercion).
- Conventional commits, one requirement per commit, reference the R-id.
- Don't rename existing tools/params; add params as optional with defaults.
- When all requirements are done, move this file to `Updates/Archive/`.

---

## R3 — Bound stream payloads  `HIGH`  `STB-H1`

**Problem.** `get_activity_streams` returns every sample of every stream verbatim — no cap or
downsampling. A 4-hour 1 Hz GPS ride ≈ 14,400 samples × up to 11 streams → multi-MB JSON that can
exceed the MCP message limit or flood context.

**Files.** `tools/activity_analysis.py` (`get_activity_streams`, ~L108–119), `client.py`
(`get_activity_streams`, ~L774).

**Implementation.**
- Add optional params: `max_samples: int | None` (default a sane cap, e.g. 3000) — decimate uniformly
  (stride sampling; keep first/last) when raw length exceeds it; `resolution: int | None` — explicit
  "every Nth sample" override.
- On decimation set `metadata.truncated=true`, `metadata.original_samples`, `metadata.returned_samples`,
  `metadata.stride`.
- Preserve the resilient-builder + dropped-streams metadata behaviour.

**Acceptance criteria.**
- [ ] A mocked 20,000-sample stream returns ≤ `max_samples` per stream with `metadata.truncated=true` and correct counts.
- [ ] `max_samples` larger than the data returns everything with `truncated=false`.
- [ ] Existing streams tests pass; latlng reshape + dropped-stream metadata unaffected.

---

## R4 — Guard download tool payloads & output paths  `HIGH`  `STB-H2`

**Problem.** `download_activity_file` / `download_fit_file` / `download_gpx_file` base64 the entire file
into the response when `output_path` is omitted (a season GPX can be 10–50 MB, +33% base64), and
`output_path` writes to arbitrary server paths (tmpfs on Cloud Run's 512 MiB budget).

**Files.** `tools/activities.py` (~L509–531 write path, L536/605/675 encode).

**Implementation.**
- Add a max inline size (e.g. `DOWNLOAD_MAX_INLINE_BYTES` ≈ 5 MB). If exceeded and no `output_path`,
  return a structured error telling the caller to supply `output_path` — not the bytes.
- Constrain `output_path` to a whitelisted scratch dir (reject absolute paths / `..` outside it);
  create the dir if needed.
- Report `metadata.bytes` and `metadata.encoding` on success.

**Acceptance criteria.**
- [ ] A mocked oversized file with no `output_path` returns a `validation_error`, not base64 bytes.
- [ ] `output_path` outside the scratch dir (absolute or `..`) is rejected.
- [ ] A small file still returns inline base64 as today.

---

## R5 — Route-similarity payload opt-out  `MEDIUM`  `STB-M6`

**Problem.** `get_route_similarity` returns both routes' full paths with no way to exclude them — same
payload class as R3, and inconsistent with `get_route` (defaults `include_path=False`).

**Files.** `client.py` (~L1405–1426), `tools/routes.py`.

**Implementation.** Add `include_paths: bool = False`; when false return only the similarity
summary/metrics; identical behaviour when true.

**Acceptance criteria.**
- [ ] Default call omits raw path arrays; `include_paths=true` restores them.
- [ ] Similarity metrics unchanged in both modes.

---

## R6 — Resilient per-item parsing for all list endpoints  `HIGH`  `STB-H3` + `STB-M4`

**Problem.** Only `get_activities` parses items defensively. Nine list endpoints use
`TypeAdapter.validate_python` atomically, so one drifted item fails the whole call — the exact
mechanism behind the latlng and SportSettings breakages. Several singleton models (`Athlete`, `Event`,
`HistogramBin`) also have required fields that hard-fail on drift.

**Files.** `client.py` — events (~L612), search (~L282), wellness (~L503), gear (~L1012), folders
(~L742), workouts (~L933), best-efforts (~L832), activities-around (~L336), search-full (~L309);
reference pattern is `get_activities` (~L228–243). Models in `models.py`.

**Implementation.**
- Factor the per-item try/except into a reusable helper, e.g.
  `parse_list_resilient(items, Model, *, label) -> tuple[list[Model], list[dict]]`; log each drop via
  `logger.warning`.
- Apply to all nine endpoints; surface `metadata.dropped_count` (and, for small N, which fields failed)
  in the corresponding tools so the LLM knows the list is partial.
- In `models.py`, default-`None` non-essential required fields on `Athlete`, `Event`, `HistogramBin`
  (mirroring `ActivitySummary`: `ConfigDict(extra="ignore")` + optional fields). Verify `Athlete.id`/
  `name` are genuinely always present before changing them.

**Acceptance criteria.**
- [ ] For each of the nine endpoints, a response with one malformed item returns the good items plus
      `metadata.dropped_count == 1` (parametrized test).
- [ ] A drifted/renamed field on a singleton model no longer raises; the tool returns a partial response.
- [ ] The shared helper is unit-tested independently.
