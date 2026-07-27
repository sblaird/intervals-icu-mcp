"""Timezone-aware "today" for date-range defaults.

Hosted servers run UTC, so naive datetime.now() shifts every "last N days" /
"today" range once the athlete's local evening crosses UTC midnight. Tools must
compute dates in the configured athlete timezone (INTERVALS_ICU_TIMEZONE).

The two test timezones sit 26 hours apart (UTC+14 vs UTC-12), so their calendar
dates ALWAYS differ — the assertions are deterministic at any wall-clock time.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.timeutil import local_now
from intervals_icu_mcp.tools.events import get_calendar_events
from intervals_icu_mcp.tools.wellness import get_wellness_data

TZ_AHEAD = "Pacific/Kiritimati"  # UTC+14
TZ_BEHIND = "Etc/GMT+12"  # UTC-12 (POSIX sign convention is inverted)


def _ctx(mock_config) -> MagicMock:
    ctx = MagicMock()
    ctx.get_state.return_value = mock_config
    return ctx


class TestLocalNow:
    def test_respects_configured_timezone(self, mock_config):
        mock_config.intervals_icu_timezone = TZ_AHEAD
        ahead = local_now(mock_config)
        mock_config.intervals_icu_timezone = TZ_BEHIND
        behind = local_now(mock_config)
        assert (ahead.date() - behind.date()).days >= 1

    def test_invalid_timezone_falls_back_to_utc(self, mock_config):
        mock_config.intervals_icu_timezone = "Not/AZone"
        now = local_now(mock_config)
        offset = now.utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 0


class TestWellnessRangeTimezone:
    async def test_range_follows_athlete_timezone(self, mock_config, respx_mock):
        """The same call made under two timezones 26h apart must produce
        different `newest` dates — a naive server-clock implementation would
        produce the same date for both."""
        captured: list[dict[str, Any]] = []

        def handler(request):
            captured.append(dict(request.url.params))
            return Response(200, json=[])

        respx_mock.get("/athlete/i123456/wellness").mock(side_effect=handler)

        mock_config.intervals_icu_timezone = TZ_AHEAD
        await get_wellness_data(days_back=7, ctx=_ctx(mock_config))
        mock_config.intervals_icu_timezone = TZ_BEHIND
        await get_wellness_data(days_back=7, ctx=_ctx(mock_config))

        assert len(captured) == 2
        assert captured[0]["newest"] != captured[1]["newest"]
        assert captured[0]["oldest"] != captured[1]["oldest"]


class TestCalendarRangeTimezone:
    async def test_range_follows_athlete_timezone(self, mock_config, respx_mock):
        captured: list[dict[str, Any]] = []

        def handler(request):
            captured.append(dict(request.url.params))
            return Response(200, json=[])

        respx_mock.get("/athlete/i123456/events").mock(side_effect=handler)

        mock_config.intervals_icu_timezone = TZ_AHEAD
        await get_calendar_events(ctx=_ctx(mock_config))
        mock_config.intervals_icu_timezone = TZ_BEHIND
        await get_calendar_events(ctx=_ctx(mock_config))

        assert len(captured) == 2
        assert captured[0]["oldest"] != captured[1]["oldest"]
        assert captured[0]["newest"] != captured[1]["newest"]


class TestTrendsDateLabels:
    async def test_trend_current_values_carry_their_source_date(self, mock_config, respx_mock):
        """`trends.*.current` may come from an older record than the newest day
        (e.g. HRV not yet synced today) — it must say which date it belongs to."""
        records = [
            {"id": "2026-07-01", "hrv": 30.0, "restingHR": 50, "weight": 70.0},
            {"id": "2026-07-02", "hrv": 25.0, "restingHR": 52, "weight": 70.5},
            {"id": "2026-07-03", "restingHR": 53},  # no HRV / weight yet today
        ]
        respx_mock.get("/athlete/i123456/wellness").mock(return_value=Response(200, json=records))
        body = json.loads(await get_wellness_data(ctx=_ctx(mock_config)))
        trends = body["data"]["trends"]

        assert trends["hrv"]["current"] == 25.0
        assert trends["hrv"]["date"] == "2026-07-02"
        assert trends["resting_hr"]["current"] == 53
        assert trends["resting_hr"]["date"] == "2026-07-03"
        assert trends["weight"]["current"] == 70.5
        assert trends["weight"]["date"] == "2026-07-02"
