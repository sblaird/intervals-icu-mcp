"""Tests for the weather, routes, and decoupling/HR tools added 2026-05-09."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.activity_analysis import get_power_vs_hr, get_time_at_hr
from intervals_icu_mcp.tools.event_management import mark_event_done
from intervals_icu_mcp.tools.events import get_calendar_events, get_upcoming_workouts
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


class TestCalendarReadsLenientDates:
    """Regression: get_calendar_events / get_upcoming_workouts crashed with
    `ValueError: unconverted data remains: T00:00:00` because they used
    strptime("%Y-%m-%d") on start_date_local, which the upstream now returns
    as a full ISO-8601 datetime ('2026-05-09T00:00:00') for events created
    after the date-validation fix.
    """

    async def test_get_calendar_events_handles_mixed_date_formats(self, mock_config, respx_mock):
        from datetime import datetime, timedelta

        today = datetime.now().date()
        events = [
            {
                "id": 1,
                # New-style: full datetime
                "start_date_local": today.isoformat() + "T08:00:00",
                "category": "WORKOUT",
                "name": "New-style event",
                "type": "Ride",
            },
            {
                "id": 2,
                # Old-style: date-only
                "start_date_local": (today + timedelta(days=1)).isoformat(),
                "category": "WORKOUT",
                "name": "Old-style event",
                "type": "Ride",
            },
        ]
        respx_mock.get("/athlete/i123456/events").mock(return_value=Response(200, json=events))
        result = await get_calendar_events(days_ahead=7, days_back=0, ctx=_ctx(mock_config))
        body = json.loads(result)
        # Both events should be present (no exception from datetime parsing).
        assert "data" in body
        assert body["data"]["summary"]["total_events"] == 2
        # Events grouped by normalized 'YYYY-MM-DD' so the new-style event
        # appears under the date string, not the full datetime.
        days = body["data"]["events_by_date"]
        today_str = today.isoformat()
        assert today_str in days
        assert any(e.get("relative_timing") == "today" for e in days[today_str])
        # IDs must be in the response so callers can update/delete events.
        all_events = [e for date_events in days.values() for e in date_events]
        assert {e["id"] for e in all_events} == {1, 2}

    async def test_get_upcoming_workouts_handles_full_datetime(self, mock_config, respx_mock):
        from datetime import datetime, timedelta

        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        events = [
            {
                "id": 5,
                "start_date_local": tomorrow + "T06:00:00",
                "category": "WORKOUT",
                "name": "Threshold intervals",
                "type": "Ride",
            },
        ]
        respx_mock.get("/athlete/i123456/events").mock(return_value=Response(200, json=events))
        result = await get_upcoming_workouts(limit=10, ctx=_ctx(mock_config))
        body = json.loads(result)
        # No exception; tomorrow's workout is returned with relative_timing="tomorrow"
        assert body["data"]["count"] == 1
        assert body["data"]["workouts"][0]["relative_timing"] == "tomorrow"
        assert body["data"]["workouts"][0]["id"] == 5


class TestBulkDeleteEndpoint:
    """Regression: bulk_delete was hitting DELETE /events/bulk, but the
    upstream router matched /events/{id} first and tried to parse 'bulk'
    as an int -> NumberFormatException 422. The correct endpoint is
    PUT /events/bulk-delete with a body of [{id}, ...] DoomedEvent records.
    """

    async def test_uses_put_bulk_delete_with_doomed_event_body(self, mock_config, respx_mock):
        from intervals_icu_mcp.client import ICUClient

        captured: dict = {}

        def handler(request):
            captured["method"] = request.method
            captured["url_path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return Response(200, json={})

        respx_mock.put("/athlete/i123456/events/bulk-delete").mock(side_effect=handler)
        async with ICUClient(mock_config) as client:
            await client.bulk_delete_events([100, 200, 300])

        assert captured["method"] == "PUT"
        assert captured["url_path"].endswith("/events/bulk-delete")
        assert captured["body"] == [{"id": 100}, {"id": 200}, {"id": 300}]


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
