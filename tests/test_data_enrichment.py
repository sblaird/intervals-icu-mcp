"""Tests for Phase 4 data enrichment (R14 Activity fields, R15 wellness
vo2max, R16 five new tools). Field names verified against openapi-spec.json.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import Response

from intervals_icu_mcp.tools.activities import get_activity_details
from intervals_icu_mcp.tools.activity_analysis import (
    _cap_payload_lists,
    get_activity_curves,
    get_activity_segments,
    get_interval_stats,
)
from intervals_icu_mcp.tools.performance import get_power_model, get_power_vs_hr_trend
from intervals_icu_mcp.tools.wellness import get_wellness_data, get_wellness_for_date


def _ctx(mock_config) -> MagicMock:
    ctx = MagicMock()
    ctx.get_state.return_value = mock_config
    return ctx


# --- R14: Activity fields -----------------------------------------------------


ENRICHED_ACTIVITY = {
    "id": "i1",
    "name": "Big Gravel Ride",
    "type": "GravelRide",
    "start_date_local": "2026-07-01T07:00:00",
    "moving_time": 7200,
    "icu_zone_times": [{"id": "Z1", "secs": 1200}, {"id": "Z2", "secs": 3600}],
    "icu_hr_zone_times": [900, 3000, 2100],
    "pace_zone_times": [600, 900],
    "gap_zone_times": [500, 800],
    "icu_ctl": 55.2,
    "icu_atl": 62.8,
    "icu_rolling_ftp": 302,
    "icu_rolling_ftp_delta": 4,
    "icu_pm_ftp": 305,
    "icu_pm_cp": 298,
    "icu_pm_w_prime": 21000,
    "decoupling": 3.4,
    "polarization_index": 1.8,
    "session_rpe": 7,
    "strain_score": 145.5,
    "power_load": 95,
    "hr_load": 88,
    "pace_load": 0,
    "icu_joules": 2650000,
    "icu_joules_above_ftp": 130000,
    "carbs_used": 210,
    "carbs_ingested": 120,
    "headwind_percent": 42.0,
    "tailwind_percent": 31.0,
    "average_wind_speed": 12.5,
    "average_temp": 18.5,
    "gap": 3.1,
    "tags": ["gravel", "race-prep"],
    "race": True,
    "coasting_time": 480,
    "interval_summary": ["2x20m @ 250w"],
}


class TestActivityEnrichment:
    async def test_enriched_fields_surface(self, mock_config, respx_mock):
        respx_mock.get("/activity/i1").mock(return_value=Response(200, json=ENRICHED_ACTIVITY))
        body = json.loads(await get_activity_details(activity_id="i1", ctx=_ctx(mock_config)))
        data = body["data"]

        assert data["zone_times"]["power"] == ENRICHED_ACTIVITY["icu_zone_times"]
        assert data["zone_times"]["heart_rate"] == [900, 3000, 2100]
        assert data["fitness"]["ctl"] == 55.2
        assert data["fitness"]["rolling_ftp"] == 302
        assert data["fitness"]["power_model_w_prime_joules"] == 21000
        assert data["training"]["decoupling_percent"] == 3.4
        assert data["training"]["polarization_index"] == 1.8
        assert data["training"]["session_rpe"] == 7
        assert data["training"]["strain_score"] == 145.5
        assert data["training"]["power_load"] == 95
        assert data["fueling"]["work_joules"] == 2650000
        assert data["fueling"]["carbs_used_grams"] == 210
        assert data["fueling"]["carbs_ingested_grams"] == 120
        assert data["environment"]["average_temp_c"] == 18.5
        assert data["environment"]["headwind_percent"] == 42.0
        assert data["other"]["race"] is True
        assert data["other"]["tags"] == ["gravel", "race-prep"]
        assert data["other"]["gap_meters_per_sec"] == 3.1
        assert data["other"]["coasting_time_seconds"] == 480
        assert data["other"]["interval_summary"] == ["2x20m @ 250w"]

    async def test_absent_fields_are_omitted(self, mock_config, respx_mock):
        minimal = {"id": "i2", "name": "Recovery spin", "start_date_local": "2026-07-02T07:00:00"}
        respx_mock.get("/activity/i2").mock(return_value=Response(200, json=minimal))
        body = json.loads(await get_activity_details(activity_id="i2", ctx=_ctx(mock_config)))
        data = body["data"]
        for section in ("zone_times", "fitness", "fueling", "environment"):
            assert section not in data


# --- R15: wellness vo2max ------------------------------------------------------


class TestWellnessVo2max:
    async def test_vo2max_in_range_data(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/wellness").mock(
            return_value=Response(200, json=[{"id": "2026-07-01", "vo2max": 52.3}])
        )
        body = json.loads(await get_wellness_data(ctx=_ctx(mock_config)))
        assert body["data"]["wellness_data"][0]["other"]["vo2max"] == 52.3

    async def test_vo2max_in_single_date(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/wellness/2026-07-01").mock(
            return_value=Response(200, json={"id": "2026-07-01", "vo2max": 52.34, "weight": 71.0})
        )
        body = json.loads(await get_wellness_for_date(date="2026-07-01", ctx=_ctx(mock_config)))
        assert body["data"]["body"]["vo2max"] == 52.3


# --- R16: five new tools ---------------------------------------------------------


class TestPowerModel:
    async def test_returns_model_payload(self, mock_config, respx_mock):
        model = {"ftp": 305, "criticalPower": 298.5, "wPrime": 21000, "pMax": 900}
        captured: dict[str, Any] = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json=model)

        respx_mock.get("/athlete/i123456/mmp-model").mock(side_effect=handler)
        body = json.loads(await get_power_model(ctx=_ctx(mock_config)))
        assert body["data"]["power_model"] == model
        assert captured["params"] == {"type": "Ride"}


class TestPowerVsHrTrend:
    async def test_passes_range_and_returns_payload(self, mock_config, respx_mock):
        payload = {"buckets": [1, 2, 3]}
        captured: dict[str, Any] = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json=payload)

        respx_mock.get("/athlete/i123456/power-hr-curve").mock(side_effect=handler)
        body = json.loads(
            await get_power_vs_hr_trend(
                start_date="2026-05-01", end_date="2026-07-01", ctx=_ctx(mock_config)
            )
        )
        assert body["data"]["power_vs_hr"] == payload
        assert captured["params"] == {"start": "2026-05-01", "end": "2026-07-01"}


class TestActivityCurves:
    async def test_power_curve_with_fatigue(self, mock_config, respx_mock):
        payload = {"secs": [1, 5, 60], "watts": [900, 700, 400]}
        captured: dict[str, Any] = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json=payload)

        respx_mock.get("/activity/i1/power-curve").mock(side_effect=handler)
        body = json.loads(
            await get_activity_curves(activity_id="i1", fatigue="1000", ctx=_ctx(mock_config))
        )
        assert body["data"]["curve"] == payload
        assert body["metadata"]["truncated"] is False
        assert captured["params"] == {"fatigue": "1000"}

    async def test_pace_curve_with_gap(self, mock_config, respx_mock):
        captured: dict[str, Any] = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json={})

        respx_mock.get("/activity/i1/pace-curve").mock(side_effect=handler)
        await get_activity_curves(
            activity_id="i1", curve_type="pace", use_gap=True, ctx=_ctx(mock_config)
        )
        assert captured["params"] == {"gap": "true"}

    async def test_long_curve_is_capped(self, mock_config, respx_mock):
        payload = {"secs": list(range(14400)), "watts": list(range(14400))}
        respx_mock.get("/activity/i1/power-curve").mock(return_value=Response(200, json=payload))
        body = json.loads(
            await get_activity_curves(activity_id="i1", max_points=100, ctx=_ctx(mock_config))
        )
        curve = body["data"]["curve"]
        assert len(curve["secs"]) <= 100
        assert curve["secs"][0] == 0 and curve["secs"][-1] == 14399
        assert body["metadata"]["truncated"] is True

    async def test_invalid_curve_type_rejected(self, mock_config):
        body = json.loads(
            await get_activity_curves(activity_id="i1", curve_type="wattage", ctx=_ctx(mock_config))
        )
        assert body["error"]["type"] == "validation_error"


class TestIntervalStats:
    async def test_passes_indices(self, mock_config, respx_mock):
        stats = {"average_watts": 250, "duration": 900}
        captured: dict[str, Any] = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json=stats)

        respx_mock.get("/activity/i1/interval-stats").mock(side_effect=handler)
        body = json.loads(
            await get_interval_stats(
                activity_id="i1", start_index=2400, end_index=3300, ctx=_ctx(mock_config)
            )
        )
        assert body["data"]["stats"] == stats
        assert captured["params"] == {"start_index": "2400", "end_index": "3300"}

    @pytest.mark.parametrize(("start", "end"), [(-1, 10), (10, 10), (10, 5)])
    async def test_bad_indices_rejected(self, mock_config, start, end):
        body = json.loads(
            await get_interval_stats(
                activity_id="i1", start_index=start, end_index=end, ctx=_ctx(mock_config)
            )
        )
        assert body["error"]["type"] == "validation_error"


class TestActivitySegments:
    async def test_returns_segments_with_count(self, mock_config, respx_mock):
        segments = [{"id": 1, "name": "Hill loop"}, {"id": 2, "name": "Sprint stretch"}]
        respx_mock.get("/activity/i1/segments").mock(return_value=Response(200, json=segments))
        body = json.loads(await get_activity_segments(activity_id="i1", ctx=_ctx(mock_config)))
        assert body["data"]["segments"] == segments
        assert body["data"]["count"] == 2


class TestCapPayloadLists:
    def test_bare_list_capped(self):
        capped, meta = _cap_payload_lists(list(range(1000)), 10)
        assert len(capped) <= 10
        assert capped[0] == 0 and capped[-1] == 999
        assert meta["truncated"] is True

    def test_short_payload_untouched(self):
        payload = {"a": [1, 2, 3], "b": "x"}
        capped, meta = _cap_payload_lists(payload, 10)
        assert capped == payload
        assert meta == {"truncated": False}


class TestRegistration:
    async def test_all_five_tools_registered(self):
        from fastmcp import Client

        import intervals_icu_mcp.server as server_module

        async with Client(server_module.mcp) as client:
            tools = {t.name for t in await client.list_tools()}
        assert {
            "get_power_model",
            "get_power_vs_hr_trend",
            "get_activity_curves",
            "get_interval_stats",
            "get_activity_segments",
        } <= tools
