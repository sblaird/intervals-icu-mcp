"""Tests for stream payload bounding (R3, STB-H1).

``get_activity_streams`` used to return every sample of every stream verbatim
— a 4-hour 1 Hz ride is ~14,400 samples x up to 11 streams, a multi-MB JSON
that can blow the MCP message limit. Streams longer than ``max_samples``
(default 3000) are now uniformly decimated (first/last kept), with truncation
metadata so the LLM knows the series was thinned.

Driven through the in-memory Client so the served schema (including the
global string-arg widening for the new int params) is exercised.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from fastmcp import Client
from httpx import Response
from mcp.types import TextContent

import intervals_icu_mcp.server as server_module
from intervals_icu_mcp.tools.activity_analysis import _decimate

mcp = server_module.mcp


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")


async def _call(args: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool("get_activity_streams", args)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return json.loads(block.text)


def _streams_payload(n: int) -> list[dict[str, Any]]:
    return [
        {"type": "watts", "data": list(range(n))},
        {"type": "time", "data": list(range(n))},
        {"type": "latlng", "data": [[44.0 + i * 1e-6, -72.0 - i * 1e-6] for i in range(n)]},
    ]


def _mock_streams(rx: respx.MockRouter, payload: Any) -> None:
    rx.route(host="intervals.icu").mock(return_value=Response(200, json=payload))


class TestDefaultCap:
    async def test_20000_samples_capped_to_default(self, configured_env):
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, _streams_payload(20_000))
            payload = await _call({"activity_id": "i1"})

        meta = payload["metadata"]
        assert meta["truncated"] is True
        assert meta["original_samples"] == 20_000
        assert meta["stride"] >= 2
        for name, values in payload["data"]["streams"].items():
            assert len(values) <= 3000, f"{name} not capped: {len(values)}"
        watts = payload["data"]["streams"]["watts"]
        assert watts[0] == 0 and watts[-1] == 19_999, "first/last samples must be kept"
        assert meta["returned_samples"] == len(watts)
        # stream_lengths reflects the returned (decimated) lengths.
        assert payload["data"]["stream_lengths"]["watts"] == len(watts)

    async def test_latlng_pairs_survive_decimation(self, configured_env):
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, _streams_payload(20_000))
            payload = await _call({"activity_id": "i1"})

        latlng = payload["data"]["streams"]["latlng"]
        assert len(latlng) <= 3000
        assert all(isinstance(pair, list) and len(pair) == 2 for pair in latlng)
        assert latlng[0] == [44.0, -72.0]

    async def test_small_activity_returned_in_full(self, configured_env):
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, _streams_payload(50))
            payload = await _call({"activity_id": "i1"})

        assert payload["metadata"]["truncated"] is False
        assert len(payload["data"]["streams"]["watts"]) == 50


class TestExplicitParams:
    async def test_custom_max_samples(self, configured_env):
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, _streams_payload(1000))
            payload = await _call({"activity_id": "i1", "max_samples": 100})

        assert payload["metadata"]["truncated"] is True
        assert len(payload["data"]["streams"]["watts"]) <= 100

    async def test_max_samples_string_form_accepted(self, configured_env):
        """The widened schema accepts the JSON-string form some clients send."""
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, _streams_payload(1000))
            payload = await _call({"activity_id": "i1", "max_samples": "100"})

        assert len(payload["data"]["streams"]["watts"]) <= 100

    async def test_max_samples_null_disables_cap(self, configured_env):
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, _streams_payload(5000))
            payload = await _call({"activity_id": "i1", "max_samples": None})

        assert payload["metadata"]["truncated"] is False
        assert len(payload["data"]["streams"]["watts"]) == 5000

    async def test_resolution_overrides_max_samples(self, configured_env):
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, _streams_payload(100))
            payload = await _call({"activity_id": "i1", "resolution": 10})

        meta = payload["metadata"]
        assert meta["truncated"] is True
        assert meta["stride"] == 10
        watts = payload["data"]["streams"]["watts"]
        # 0, 10, ..., 90 plus the kept last sample (99).
        assert watts == list(range(0, 100, 10)) + [99]

    @pytest.mark.parametrize("args", [{"max_samples": 0}, {"resolution": 0}, {"max_samples": -5}])
    async def test_invalid_params_return_validation_error(self, configured_env, args):
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, _streams_payload(10))
            payload = await _call({"activity_id": "i1", **args})

        assert payload["error"]["type"] == "validation_error"


class TestInteractionWithResilientParsing:
    async def test_dropped_stream_metadata_coexists_with_truncation(self, configured_env):
        payload_in = _streams_payload(20_000)
        # Flat garbage latlng — the resilient builder must drop it while the
        # good streams still come through (and get decimated).
        payload_in[2] = {"type": "latlng", "data": [1.0, 2.0, 3.0]}
        with respx.mock(assert_all_called=False) as rx:
            _mock_streams(rx, payload_in)
            payload = await _call({"activity_id": "i1"})

        meta = payload["metadata"]
        assert meta["partial"] is True
        assert "latlng" in meta["dropped_streams"]
        assert meta["truncated"] is True
        assert len(payload["data"]["streams"]["watts"]) <= 3000


class TestDecimateHelper:
    def test_stride_one_is_identity(self):
        assert _decimate([1, 2, 3], 1) == [1, 2, 3]

    def test_keeps_first_and_last(self):
        values = list(range(11))
        out = _decimate(values, 3)
        assert out[0] == 0 and out[-1] == 10

    def test_last_not_duplicated_when_on_stride(self):
        values = list(range(0, 10))  # last index 9, stride 3 hits it
        out = _decimate(values, 3)
        assert out == [0, 3, 6, 9]

    def test_tiny_lists_untouched(self):
        assert _decimate([1, 2], 5) == [1, 2]
