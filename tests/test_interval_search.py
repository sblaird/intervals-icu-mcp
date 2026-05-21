"""Tests pinning the params sent to /activities/interval-search.

The Intervals.icu API requires `minSecs`, `maxSecs`, `minIntensity`, and
`maxIntensity` on every call. Omitting any of them returns:

    HTTP 422: Required request parameter 'X' for method parameter type int is not present

These tests pin the wide-open defaults so the regression can't return silently
(originally reported 2026-05-21).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.activity_analysis import search_intervals


def _ctx(mock_config) -> MagicMock:
    ctx = MagicMock()
    ctx.get_state.return_value = mock_config
    return ctx


class TestSearchIntervalsAlwaysSendsRequiredParams:
    async def test_no_args_still_includes_intensity_and_secs(self, mock_config, respx_mock):
        captured: dict = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json=[])

        respx_mock.get("/athlete/i123456/activities/interval-search").mock(side_effect=handler)

        await search_intervals(ctx=_ctx(mock_config))

        params = captured["params"]
        # All four were the cause of the 422 — they must be present.
        assert params["minSecs"] == "0"
        assert params["maxSecs"] == "86400"
        assert params["minIntensity"] == "0"
        assert params["maxIntensity"] == "1000"
        assert params["type"] == "Ride"

    async def test_caller_supplies_duration_and_intensity(self, mock_config, respx_mock):
        captured: dict = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json=[])

        respx_mock.get("/athlete/i123456/activities/interval-search").mock(side_effect=handler)

        await search_intervals(
            interval_type="THRESHOLD",
            min_duration=300,
            max_duration=1200,
            min_intensity=90,
            max_intensity=110,
            limit=30,
            ctx=_ctx(mock_config),
        )

        params = captured["params"]
        assert params["minSecs"] == "300"
        assert params["maxSecs"] == "1200"
        assert params["minIntensity"] == "90"
        assert params["maxIntensity"] == "110"
        assert params["intervalType"] == "THRESHOLD"
        assert params["limit"] == "30"

    async def test_reps_only_sent_when_provided(self, mock_config, respx_mock):
        captured: dict = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json=[])

        respx_mock.get("/athlete/i123456/activities/interval-search").mock(side_effect=handler)

        await search_intervals(min_reps=3, max_reps=8, ctx=_ctx(mock_config))

        params = captured["params"]
        assert params["minReps"] == "3"
        assert params["maxReps"] == "8"

    async def test_reps_absent_when_not_supplied(self, mock_config, respx_mock):
        captured: dict = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json=[])

        respx_mock.get("/athlete/i123456/activities/interval-search").mock(side_effect=handler)

        await search_intervals(ctx=_ctx(mock_config))

        params = captured["params"]
        assert "minReps" not in params
        assert "maxReps" not in params

    async def test_returns_results_with_intensity_in_criteria(self, mock_config, respx_mock):
        intervals_payload = [
            {"activity_id": "i111", "type": "THRESHOLD", "secs": 600, "intensity": 95},
            {"activity_id": "i222", "type": "THRESHOLD", "secs": 900, "intensity": 102},
        ]
        respx_mock.get("/athlete/i123456/activities/interval-search").mock(
            return_value=Response(200, json=intervals_payload)
        )

        result = await search_intervals(
            interval_type="THRESHOLD",
            min_intensity=90,
            max_intensity=110,
            ctx=_ctx(mock_config),
        )
        body = json.loads(result)

        assert body["data"]["count"] == 2
        criteria = body["data"]["search_criteria"]
        assert criteria["min_intensity_pct"] == 90
        assert criteria["max_intensity_pct"] == 110
