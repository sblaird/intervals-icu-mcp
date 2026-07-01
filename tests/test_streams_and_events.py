"""Tests for stream parsing resilience and create_event validation.

Covers issues from the 2026-07-01 connector issues log:
- Issue 3: a malformed `latlng` stream must not take down the whole response.
- Issue 4: WORKOUT events must fail fast with a clear message when no type is given.
"""

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.activity_analysis import get_activity_streams
from intervals_icu_mcp.tools.event_management import create_event


class TestActivityStreamsResilience:
    """Streams should survive malformed per-stream data (Issue 3)."""

    async def test_flat_latlng_is_reshaped_into_pairs(self, mock_config, respx_mock):
        """A flat [lat, lng, lat, lng, ...] latlng stream is reshaped, not crashed."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        payload = [
            {"type": "watts", "data": [100, 110, 120, 130]},
            # Intervals.icu sometimes returns latlng as a flat float list rather
            # than a list of [lat, lng] pairs. This previously failed validation
            # on every sample and discarded the entire streams response.
            {"type": "latlng", "data": [51.5094, -0.1000, 51.5095, -0.1001]},
        ]
        respx_mock.get("/activity/i161747339/streams").mock(
            return_value=Response(200, json=payload)
        )

        result = await get_activity_streams(activity_id="i161747339", ctx=mock_ctx)
        response = json.loads(result)

        assert "error" not in response, result
        streams = response["data"]["streams"]
        # watts survives untouched
        assert streams["watts"] == [100, 110, 120, 130]
        # latlng reshaped into pairs
        assert streams["latlng"] == [[51.5094, -0.1000], [51.5095, -0.1001]]

    async def test_unparseable_stream_dropped_but_others_survive(self, mock_config, respx_mock):
        """One bad stream is dropped; the good ones still come through."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        payload = [
            {"type": "watts", "data": [200, 210, 220]},
            # Garbage that cannot be coerced to list[float] pairs (odd length,
            # non-numeric). Must not blow up the whole response.
            {"type": "latlng", "data": ["not", "a", "coordinate"]},
        ]
        respx_mock.get("/activity/i999/streams").mock(return_value=Response(200, json=payload))

        result = await get_activity_streams(activity_id="i999", ctx=mock_ctx)
        response = json.loads(result)

        assert "error" not in response, result
        streams = response["data"]["streams"]
        assert streams["watts"] == [200, 210, 220]
        assert "latlng" not in streams

    async def test_dropped_stream_reported_in_metadata(self, mock_config, respx_mock):
        """A dropped stream is surfaced in response metadata so the LLM knows it's partial (Issue #3 follow-on)."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        payload = [
            {"type": "watts", "data": [200, 210, 220]},
            {"type": "latlng", "data": ["not", "a", "coordinate"]},
        ]
        respx_mock.get("/activity/i999/streams").mock(return_value=Response(200, json=payload))

        result = await get_activity_streams(activity_id="i999", ctx=mock_ctx)
        response = json.loads(result)

        assert "error" not in response, result
        # Good stream still comes through.
        assert response["data"]["streams"]["watts"] == [200, 210, 220]
        # The dropped stream is reported in metadata.
        meta = response["metadata"]
        assert meta["partial"] is True
        assert meta["dropped_streams"] == ["latlng"]

    async def test_clean_streams_have_no_partial_metadata(self, mock_config, respx_mock):
        """When nothing is dropped, no partial/dropped_streams metadata is emitted."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        payload = [{"type": "watts", "data": [100, 110, 120]}]
        respx_mock.get("/activity/i999/streams").mock(return_value=Response(200, json=payload))

        result = await get_activity_streams(activity_id="i999", ctx=mock_ctx)
        response = json.loads(result)

        assert "error" not in response, result
        meta = response["metadata"]
        assert "partial" not in meta
        assert "dropped_streams" not in meta


class TestCreateEventValidation:
    """WORKOUT events need a type; fail fast with a clear message (Issue 4)."""

    async def test_workout_without_type_returns_clear_error(self, mock_config, respx_mock):
        """category=WORKOUT with no event_type returns a validation error, not a raw 422."""
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        result = await create_event(
            start_date="2026-07-02",
            name="Threshold Intervals",
            category="WORKOUT",
            ctx=mock_ctx,
        )
        response = json.loads(result)

        assert response["error"]["type"] == "validation_error", result
        assert "event_type" in response["error"]["message"]
