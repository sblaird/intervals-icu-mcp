# Update 05 — Data Enrichment (Phase 4)

**Source:** connector review, data-coverage gaps. **D4 resolved: ship everything** — R14/R15 field
additions and **all five** R16 tools. Full context in `docs/connector-review-fixes.md`.

Cheapest, highest-value first: extra fields on endpoints already fetched. All new int/array params
inherit the global schema widening automatically — no per-param coercion needed.

## Guardrails (apply to every requirement)
- Test-first. `make can-release` must stay green (full suite, ruff check+format, **pyright 0 errors**).
- New tools register in `server.py` (`mcp.tool()(fn)`), follow the standard tool skeleton, and return
  `ResponseBuilder` responses. Mind payload size for any curve/path-bearing response (cap or summarize,
  per Update 02's R3/R4 spirit).
- Conventional commits, one per R-id. When done, move this file to `Updates/Archive/`.

---

## R14 — Surface untapped Activity fields  `TRIVIAL`

**Requirement.** `get_activity_details` exposes the training-relevant Activity fields currently dropped
(only 38 of 173 are surfaced today).

**Files.** `models.py` (`Activity`), `tools/activities.py` (`get_activity_details`).

**Fields to add** (all optional, `extra="ignore"`):
- **Zones:** `icu_zone_times`, `icu_hr_zone_times`, `pace_zone_times`, `gap_zone_times`
- **Fitness/power model:** `icu_ctl`, `icu_atl`, `icu_rolling_ftp`, `icu_rolling_ftp_delta`, `icu_pm_ftp`, `icu_pm_cp`, `icu_pm_w_prime`
- **Load/intensity:** `decoupling`, `polarization_index`, `session_rpe`, `strain_score`, `power_load`, `hr_load`, `pace_load`
- **Fueling:** `icu_joules`, `icu_joules_above_ftp`, `carbs_used`
- **Environment:** `headwind_percent`, `tailwind_percent`, `average_wind_speed`, `average_temp`
- **Meta:** `gap`, `tags`, `race`, `coasting_time`, `interval_summary`

**Acceptance criteria.**
- [ ] A mocked activity payload carrying these fields surfaces them in `get_activity_details`.
- [ ] Absent fields are omitted (not forced to null), consistent with existing serialization.

---

## R15 — Add Wellness `vo2max` (and minor fields)  `TRIVIAL`

**Files.** `models.py` (`Wellness`), `tools/wellness.py`.

**Requirement.** `vo2max` (Garmin-synced) is surfaced; optionally `tempRestingHR`, `tempWeight`.

**Acceptance criteria.**
- [ ] A wellness payload with `vo2max` surfaces it in `get_wellness_data` / `get_wellness_for_date`.

---

## R16 — New tools for high-value endpoints  `SMALL each`  (D4: ship all five)

Implement all five on the existing tool pattern; register each in `server.py`. Independent — one commit
per tool is fine.

- [ ] **`get_power_model` / eFTP** — `GET /athlete/{id}/mmp-model?type=Ride` → `ftp`, `criticalPower`,
      `wPrime`, `pMax`. Answers "is my FTP trending up?".
- [ ] **Per-activity curves with fatigue** — `GET /activity/{id}/power-curve?fatigue=…` (+ hr/pace
      variants). "Season-best effort?" and fatigue-resistance (curve after kJ pre-burned).
- [ ] **`get_power_vs_hr_trend`** — `GET /athlete/{id}/power-hr-curve?start&end`. Aerobic-efficiency
      trend across blocks.
- [ ] **`get_interval_stats`** — `GET /activity/{id}/interval-stats?start_index&end_index`. Arbitrary
      span analysis ("the climb from minute 40–55"); takes stream indices.
- [ ] **`get_activity_segments`** — `GET /activity/{id}/segments`. Repeated-loop benchmarking.

**Acceptance criteria (per tool).**
- [ ] Registered in `server.py`; returns a `ResponseBuilder` response; mocked-endpoint test passes.
- [ ] Payload-size sanity for any curve/path-bearing response.

**Honorable mentions (only if wanted):** `/activity-tags` for search filters, planned-workout export
(zwo/erg/fit), `/activities.csv` bulk export, power-meter battery health, bulk fetch by ids,
fitness-model events. **Non-goal:** CTL/ATL time series already comes from the daily wellness list — no
new endpoint needed. The unused `AthleteTrainingPlan` model is dead code — remove or wire up.
