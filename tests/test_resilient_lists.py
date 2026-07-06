"""Tests for resilient per-item list parsing (R6, STB-H3 + STB-M4).

Every list endpoint used to validate atomically — one drifted item from
upstream failed the entire call (the exact mechanism behind the latlng and
SportSettings breakages). Now each item validates independently: good items
come through, drops are logged, and tools surface metadata.dropped_count so
the LLM knows the list is partial.

Also covers the drift-tolerant singleton models (Athlete, Event,
HistogramBin) and the shared parse_list_resilient helper.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from fastmcp import Client
from httpx import Response
from mcp.types import TextContent
from pydantic import BaseModel

import intervals_icu_mcp.server as server_module
from intervals_icu_mcp.client import (
    ICUAPIError,
    dropped_items_metadata,
    parse_list_resilient,
)
from intervals_icu_mcp.models import Athlete, Event, Histogram

mcp = server_module.mcp

BAD_ID_ITEM = {"id": {"nested": "garbage"}}


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")


async def _call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(tool_name, args)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return json.loads(block.text)


# (tool, args, payload-with-one-malformed-item)
ENDPOINT_CASES = [
    (
        "get_recent_activities",
        {},
        [
            {"id": "1", "name": "Ride", "start_date_local": "2026-07-01T07:00:00", "type": "Ride"},
            BAD_ID_ITEM,
        ],
    ),
    (
        "search_activities",
        {"query": "ride"},
        [
            {"id": "1", "name": "Ride", "start_date_local": "2026-07-01T07:00:00"},
            BAD_ID_ITEM,
        ],
    ),
    (
        "search_activities_full",
        {"query": "ride"},
        [
            {"id": "1", "name": "Ride", "start_date_local": "2026-07-01T07:00:00", "type": "Ride"},
            BAD_ID_ITEM,
        ],
    ),
    (
        "get_activities_around",
        {"activity_id": "1"},
        [
            {"id": "1", "name": "Ride", "start_date_local": "2026-07-01T07:00:00", "type": "Ride"},
            BAD_ID_ITEM,
        ],
    ),
    (
        "get_wellness_data",
        {},
        [{"id": "2026-07-01", "restingHR": 45}, BAD_ID_ITEM],
    ),
    (
        "get_calendar_events",
        {},
        [
            {
                "id": 1,
                "start_date_local": "2026-07-01T00:00:00",
                "category": "WORKOUT",
                "name": "W",
            },
            BAD_ID_ITEM,
        ],
    ),
    (
        "get_workout_library",
        {},
        [{"id": 1, "name": "Base plan"}, BAD_ID_ITEM],
    ),
    (
        "get_workouts_in_folder",
        {"folder_id": 1},
        [{"id": 1, "name": "Sweet spot"}, BAD_ID_ITEM],
    ),
    (
        "get_best_efforts",
        {"activity_id": "a1"},
        [{"name": "5s", "elapsed_time": 5}, {"name": {"nested": "garbage"}}],
    ),
    (
        "get_gear_list",
        {},
        [{"id": "g1", "name": "Gravel bike"}, BAD_ID_ITEM],
    ),
    (
        "get_sport_settings",
        {},
        [{"id": 1, "types": ["Ride"], "ftp": 300}, {"types": {"nested": "garbage"}}],
    ),
]


class TestOneMalformedItemPerEndpoint:
    @pytest.mark.parametrize(
        ("tool_name", "args", "payload"),
        ENDPOINT_CASES,
        ids=[case[0] for case in ENDPOINT_CASES],
    )
    async def test_good_items_survive_with_dropped_count(
        self, configured_env, tool_name, args, payload
    ):
        with respx.mock(assert_all_called=False) as rx:
            rx.route(host="intervals.icu").mock(return_value=Response(200, json=payload))
            body = await _call(tool_name, args)

        assert "error" not in body, f"{tool_name} failed outright: {body}"
        meta = body["metadata"]
        assert meta["dropped_count"] == 1, f"{tool_name}: {meta}"
        assert meta["partial"] is True
        # The good item(s) still came through — the response has real data.
        assert body["data"], f"{tool_name} returned no data"

    async def test_intervals_wrapper_payload_drops_bad_item(self, configured_env):
        payload = {
            "id": "a1",
            "icu_intervals": [
                {"id": 1, "type": "WORK", "average_watts": 250},
                BAD_ID_ITEM,
            ],
        }
        with respx.mock(assert_all_called=False) as rx:
            rx.route(host="intervals.icu").mock(return_value=Response(200, json=payload))
            body = await _call("get_activity_intervals", {"activity_id": "a1"})

        assert body["metadata"]["dropped_count"] == 1
        assert body["data"]["summary"]["total_intervals"] == 1

    async def test_clean_lists_have_no_partial_metadata(self, configured_env):
        payload = [{"id": "g1", "name": "Gravel bike"}]
        with respx.mock(assert_all_called=False) as rx:
            rx.route(host="intervals.icu").mock(return_value=Response(200, json=payload))
            body = await _call("get_gear_list", {})

        assert "dropped_count" not in body.get("metadata", {})


class TestSingletonModelDrift:
    def test_athlete_survives_missing_name_and_unknown_fields(self):
        athlete = Athlete.model_validate({"id": "i1", "future_field": {"x": 1}, "ctl": 50.0})
        assert athlete.id == "i1"
        assert athlete.name is None

    def test_event_survives_missing_start_date(self):
        event = Event.model_validate({"id": 7, "category": "NOTE", "unknown_field": True})
        assert event.id == 7
        assert event.start_date_local is None

    def test_histogram_survives_drifted_bin(self):
        histogram = Histogram.model_validate(
            {"bins": [{"min": 0.0, "max": 100.0, "count": 5}, {"count": None, "weird": 1}]}
        )
        assert len(histogram.bins) == 2
        assert histogram.bins[1].min is None

    async def test_histogram_tool_skips_drifted_bins(self, configured_env):
        payload = {
            "bins": [
                {"min": 100, "max": 200, "count": 10, "secs": 600},
                {"weird": True},
            ],
            "total_count": 10,
        }
        with respx.mock(assert_all_called=False) as rx:
            rx.route(host="intervals.icu").mock(return_value=Response(200, json=payload))
            body = await _call("get_power_histogram", {"activity_id": "a1"})

        assert "error" not in body
        bins = body["data"]["bins"]
        assert len(bins) == 1
        assert bins[0]["power_range"] == {"min_watts": 100, "max_watts": 200}


class _Item(BaseModel):
    id: int
    name: str | None = None


class TestParseListResilientHelper:
    def test_all_good(self):
        items, dropped = parse_list_resilient(
            [{"id": 1}, {"id": 2, "name": "b"}], _Item, label="item"
        )
        assert [i.id for i in items] == [1, 2]
        assert dropped == []

    def test_drops_only_bad_items_with_field_info(self):
        items, dropped = parse_list_resilient(
            [{"id": 1}, {"id": "not-an-int-at-all"}, {"id": 3, "name": [1]}],
            _Item,
            label="item",
        )
        assert [i.id for i in items] == [1]
        assert [d["index"] for d in dropped] == [1, 2]
        assert dropped[0]["fields"] == ["id"]
        assert dropped[1]["fields"] == ["name"]

    def test_non_list_payload_raises_api_error(self):
        with pytest.raises(ICUAPIError, match="Expected a list"):
            parse_list_resilient({"error": "nope"}, _Item, label="item")

    def test_dropped_items_metadata_empty_when_clean(self):
        assert dropped_items_metadata([], label="item") == {}

    def test_dropped_items_metadata_small_n_includes_fields(self):
        meta = dropped_items_metadata([{"index": 4, "fields": ["id"]}], label="event")
        assert meta["dropped_count"] == 1
        assert meta["partial"] is True
        assert meta["dropped_items"] == [{"index": 4, "fields": ["id"]}]
        assert "event" in meta["message"]

    def test_dropped_items_metadata_large_n_omits_details(self):
        dropped = [{"index": i, "fields": ["id"]} for i in range(10)]
        meta = dropped_items_metadata(dropped, label="item")
        assert meta["dropped_count"] == 10
        assert "dropped_items" not in meta
