"""Tests for event management tools — date/datetime normalization and write paths."""

import json
from unittest.mock import MagicMock

import pytest
from httpx import Response

from intervals_icu_mcp.tools.event_management import (
    _normalize_event_datetime,
    bulk_create_events,
    create_event,
    duplicate_event,
    update_event,
)


class TestNormalizeEventDatetime:
    def test_date_only_padded_to_midnight(self):
        assert _normalize_event_datetime("2026-05-07") == "2026-05-07T00:00:00"

    def test_full_datetime_passes_through(self):
        assert _normalize_event_datetime("2026-05-07T06:00:00") == "2026-05-07T06:00:00"

    def test_datetime_with_milliseconds_truncated(self):
        assert _normalize_event_datetime("2026-05-07T06:00:00.123") == "2026-05-07T06:00:00"

    def test_datetime_with_z_suffix_strips_tz(self):
        assert _normalize_event_datetime("2026-05-07T06:00:00Z") == "2026-05-07T06:00:00"

    def test_datetime_with_offset_strips_tz(self):
        assert _normalize_event_datetime("2026-05-07T06:00:00-04:00") == "2026-05-07T06:00:00"

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            _normalize_event_datetime("not a date")

    def test_slashes_raises(self):
        with pytest.raises(ValueError, match="Invalid date format"):
            _normalize_event_datetime("2026/05/07")


class TestCreateEvent:
    async def test_date_only_input_normalized_in_request_body(
        self,
        mock_config,
        respx_mock,
        mock_event_data,
    ):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json=mock_event_data)

        respx_mock.post("/athlete/i123456/events").mock(side_effect=handler)

        result = await create_event(
            start_date="2026-05-07",
            name="Threshold Intervals",
            category="WORKOUT",
            ctx=mock_ctx,
        )

        body = captured["body"]
        assert isinstance(body, dict)
        assert body["start_date_local"] == "2026-05-07T00:00:00"
        assert body["name"] == "Threshold Intervals"
        assert body["category"] == "WORKOUT"

        response = json.loads(result)
        assert "data" in response

    async def test_datetime_input_passed_through(
        self,
        mock_config,
        respx_mock,
        mock_event_data,
    ):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json=mock_event_data)

        respx_mock.post("/athlete/i123456/events").mock(side_effect=handler)

        await create_event(
            start_date="2026-05-07T06:00:00",
            name="AM Ride",
            category="WORKOUT",
            ctx=mock_ctx,
        )
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["start_date_local"] == "2026-05-07T06:00:00"

    async def test_invalid_date_returns_validation_error(self, mock_config):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        result = await create_event(
            start_date="garbage",
            name="x",
            category="WORKOUT",
            ctx=mock_ctx,
        )
        response = json.loads(result)
        assert response["error"]["type"] == "validation_error"
        assert "Invalid date format" in response["error"]["message"]


class TestUpdateEvent:
    async def test_date_only_normalized_when_updating(
        self,
        mock_config,
        respx_mock,
        mock_event_data,
    ):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json=mock_event_data)

        respx_mock.put("/athlete/i123456/events/1001").mock(side_effect=handler)

        await update_event(
            event_id=1001,
            start_date="2026-05-07",
            ctx=mock_ctx,
        )
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["start_date_local"] == "2026-05-07T00:00:00"

    async def test_no_start_date_means_no_date_in_body(
        self,
        mock_config,
        respx_mock,
        mock_event_data,
    ):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json=mock_event_data)

        respx_mock.put("/athlete/i123456/events/1001").mock(side_effect=handler)

        await update_event(event_id=1001, name="renamed", ctx=mock_ctx)
        body = captured["body"]
        assert isinstance(body, dict)
        assert "start_date_local" not in body
        assert body["name"] == "renamed"


class TestBulkCreateEvents:
    async def test_each_event_normalized(
        self,
        mock_config,
        respx_mock,
        mock_event_data,
    ):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json=[mock_event_data, mock_event_data])

        respx_mock.post("/athlete/i123456/events/bulk").mock(side_effect=handler)

        events_json = json.dumps(
            [
                {
                    "start_date_local": "2026-05-07",
                    "name": "Day 1",
                    "category": "WORKOUT",
                },
                {
                    "start_date_local": "2026-05-08T06:30:00",
                    "name": "Day 2",
                    "category": "workout",
                },
            ]
        )

        await bulk_create_events(events=events_json, ctx=mock_ctx)

        body = captured["body"]
        assert isinstance(body, list)
        assert body[0]["start_date_local"] == "2026-05-07T00:00:00"
        assert body[1]["start_date_local"] == "2026-05-08T06:30:00"
        assert body[0]["category"] == "WORKOUT"
        assert body[1]["category"] == "WORKOUT"

    async def test_invalid_date_in_one_event_returns_validation_error(self, mock_config):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        events_json = json.dumps(
            [
                {"start_date_local": "2026-05-07", "name": "ok", "category": "WORKOUT"},
                {"start_date_local": "bad", "name": "broken", "category": "WORKOUT"},
            ]
        )
        result = await bulk_create_events(events=events_json, ctx=mock_ctx)
        response = json.loads(result)
        assert response["error"]["type"] == "validation_error"
        assert "Event 1" in response["error"]["message"]


class TestDuplicateEvent:
    async def test_duplicate_normalizes_date(
        self,
        mock_config,
        respx_mock,
        mock_event_data,
    ):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json=mock_event_data)

        respx_mock.post("/athlete/i123456/events/1001/duplicate").mock(side_effect=handler)

        await duplicate_event(event_id=1001, new_date="2026-05-07", ctx=mock_ctx)

        body = captured["body"]
        assert isinstance(body, dict)
        assert body["start_date_local"] == "2026-05-07T00:00:00"

    async def test_duplicate_invalid_date(self, mock_config):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        result = await duplicate_event(event_id=1001, new_date="bad", ctx=mock_ctx)
        response = json.loads(result)
        assert response["error"]["type"] == "validation_error"
