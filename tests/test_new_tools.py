"""Tests for the weather, routes, and decoupling/HR tools added 2026-05-09."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.activity_analysis import get_power_vs_hr, get_time_at_hr
from intervals_icu_mcp.tools.event_management import mark_event_done
from intervals_icu_mcp.tools.routes import (
    compare_route_similarity,
    get_route,
    list_routes,
)
from intervals_icu_mcp.tools.weather import get_activity_weather, get_weather_forecast


def _ctx(mock_config) -> MagicMock:
    ctx = MagicMock()
    ctx.get_state.return_value = mock_config
    return ctx


class TestWeatherForecast:
    async def test_returns_forecast_payload(self, mock_config, respx_mock):
        forecast = {"events": [{"id": 1, "temp_c": 22}], "issued_at": "2026-05-09T10:00:00Z"}
        respx_mock.get("/athlete/i123456/weather-forecast").mock(
            return_value=Response(200, json=forecast)
        )
        result = await get_weather_forecast(ctx=_ctx(mock_config))
        body = json.loads(result)
        assert body["data"]["forecast"] == forecast


class TestActivityWeather:
    async def test_no_indices(self, mock_config, respx_mock):
        summary = {"avg_temp_c": 18, "max_wind_kph": 22}
        captured: dict = {}

        def handler(request):
            captured["url"] = str(request.url)
            return Response(200, json=summary)

        respx_mock.get("/activity/abc/weather-summary").mock(side_effect=handler)
        result = await get_activity_weather(activity_id="abc", ctx=_ctx(mock_config))
        body = json.loads(result)
        assert body["data"]["weather"] == summary
        assert "start_index" not in captured["url"]

    async def test_with_indices_passes_query_params(self, mock_config, respx_mock):
        captured: dict = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json={})

        respx_mock.get("/activity/abc/weather-summary").mock(side_effect=handler)
        await get_activity_weather(
            activity_id="abc",
            start_index=100,
            end_index=2000,
            ctx=_ctx(mock_config),
        )
        assert captured["params"] == {"start_index": "100", "end_index": "2000"}


class TestListRoutes:
    async def test_returns_routes_with_count(self, mock_config, respx_mock):
        routes = [
            {"id": 1, "name": "Loop A", "activity_count": 12},
            {"id": 2, "name": "Loop B", "activity_count": 3},
        ]
        respx_mock.get("/athlete/i123456/routes").mock(return_value=Response(200, json=routes))
        result = await list_routes(ctx=_ctx(mock_config))
        body = json.loads(result)
        assert body["data"]["count"] == 2
        assert body["data"]["routes"] == routes


class TestGetRoute:
    async def test_default_no_path_param(self, mock_config, respx_mock):
        captured: dict = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json={"id": 7})

        respx_mock.get("/athlete/i123456/routes/7").mock(side_effect=handler)
        result = await get_route(route_id=7, ctx=_ctx(mock_config))
        body = json.loads(result)
        assert body["data"]["route"] == {"id": 7}
        # include_path defaults to False -> includePath query NOT sent
        assert "includePath" not in captured["params"]

    async def test_include_path_passes_param(self, mock_config, respx_mock):
        captured: dict = {}

        def handler(request):
            captured["params"] = dict(request.url.params)
            return Response(200, json={"id": 7, "path": [[0, 0]]})

        respx_mock.get("/athlete/i123456/routes/7").mock(side_effect=handler)
        await get_route(route_id=7, include_path=True, ctx=_ctx(mock_config))
        assert captured["params"] == {"includePath": "true"}


class TestRouteSimilarity:
    async def test_calls_correct_path(self, mock_config, respx_mock):
        sim = {"similarity": 0.92}
        respx_mock.get("/athlete/i123456/routes/3/similarity/4").mock(
            return_value=Response(200, json=sim)
        )
        result = await compare_route_similarity(route_id=3, other_route_id=4, ctx=_ctx(mock_config))
        body = json.loads(result)
        assert body["data"]["route_id"] == 3
        assert body["data"]["other_route_id"] == 4
        assert body["data"]["similarity"] == sim


class TestPowerVsHr:
    async def test_returns_plot(self, mock_config, respx_mock):
        plot = {"points": [{"watts": 150, "hr": 130}]}
        respx_mock.get("/activity/xyz/power-vs-hr").mock(return_value=Response(200, json=plot))
        result = await get_power_vs_hr(activity_id="xyz", ctx=_ctx(mock_config))
        body = json.loads(result)
        assert body["data"]["plot"] == plot


class TestTimeAtHr:
    async def test_returns_plot(self, mock_config, respx_mock):
        plot = {"buckets": [{"hr": 130, "secs": 600}]}
        respx_mock.get("/activity/xyz/time-at-hr").mock(return_value=Response(200, json=plot))
        result = await get_time_at_hr(activity_id="xyz", ctx=_ctx(mock_config))
        body = json.loads(result)
        assert body["data"]["plot"] == plot


class TestMarkEventDone:
    async def test_posts_and_returns_activity(self, mock_config, respx_mock):
        activity = {"id": "999", "name": "Marked Done"}
        respx_mock.post("/athlete/i123456/events/42/mark-done").mock(
            return_value=Response(200, json=activity)
        )
        result = await mark_event_done(event_id=42, ctx=_ctx(mock_config))
        body = json.loads(result)
        assert body["data"]["event_id"] == 42
        assert body["data"]["activity"] == activity


class TestActivitySummaryDefensive:
    """The lenient ActivitySummary should tolerate missing id / start_date_local
    and ignore unknown fields, so a single bad upstream entry can't poison the
    whole batch.

    This guards against the get_recent_activities regression where strict
    validation against drifted upstream data caused every call to fail.
    """

    async def test_get_activities_skips_invalid_items(self, mock_config, respx_mock):
        from intervals_icu_mcp.client import ICUClient

        # Mix of: valid, missing id (now allowed), unknown extras, unparseable date
        items = [
            {"id": "1", "start_date_local": "2026-05-08T08:00:00", "name": "Valid"},
            {
                "id": "2",
                "start_date_local": "2026-05-07T07:00:00",
                "name": "Has extras",
                "future_field_we_dont_know": True,
                "another_unknown": [1, 2, 3],
            },
            {
                # bad start_date_local — should be skipped, not crash the batch
                "id": "3",
                "start_date_local": "not-a-date",
                "name": "Bad date",
            },
        ]
        respx_mock.get("/athlete/i123456/activities").mock(return_value=Response(200, json=items))
        async with ICUClient(mock_config) as client:
            activities = await client.get_activities()

        # First two parse successfully; bad-date one is dropped.
        assert len(activities) == 2
        assert activities[0].id == "1"
        assert activities[1].id == "2"
