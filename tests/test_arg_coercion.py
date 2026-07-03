"""Tests for lenient argument coercion (Issues #1 & #2, 2026-07-01 log).

Some MCP clients send integer / array tool arguments as JSON strings ("3",
'["watts","heartrate"]'). The low-level MCP SDK validates arguments against each
tool's inputSchema with strict jsonschema *before* pydantic runs, so bare int /
list[str] params reject the string form:

    Input validation error: '3' is not of type 'integer'
    Input validation error: '["watts",...]' is not valid under any of the given schemas

These tests drive the affected tools through the FULL FastMCP tool-call path
(the in-memory Client runs the same jsonschema + pydantic validation the real
connector hits) and prove the string form now succeeds and behaves identically
to the native-typed call. Calling the plain Python function would bypass schema
validation and not exercise the fix.
"""

import json

import pytest
import respx
from fastmcp import Client
from httpx import Response
from mcp.types import TextContent

import intervals_icu_mcp.server as server_module

mcp = server_module.mcp


@pytest.fixture
def configured_env(monkeypatch):
    """Provide valid (non-placeholder) credentials via environment."""
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")


async def _call(tool_name: str, args: dict) -> dict:
    """Invoke a tool via the in-memory Client and return the parsed JSON payload."""
    async with Client(mcp) as client:
        result = await client.call_tool(tool_name, args)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return json.loads(block.text)


@pytest.mark.parametrize(
    ("tool_name", "string_args", "native_args"),
    [
        ("get_recent_activities", {"days_back": "1", "limit": "3"}, {"days_back": 1, "limit": 3}),
        ("get_calendar_events", {"days_ahead": "14"}, {"days_ahead": 14}),
        ("get_wellness_data", {"days_back": "3"}, {"days_back": 3}),
    ],
)
async def test_string_integers_coerced_and_match_native(
    configured_env, tool_name, string_args, native_args
):
    """A JSON-string integer arg succeeds and yields the same data as the native int."""
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.route(host="intervals.icu").mock(return_value=Response(200, json=[]))

        string_response = await _call(tool_name, string_args)
        native_response = await _call(tool_name, native_args)

    assert "error" not in string_response, string_response
    assert "error" not in native_response, native_response
    # Coerced string call behaves identically to the native-typed call.
    assert string_response["data"] == native_response["data"]


async def test_streams_json_string_coerced_and_matches_native(configured_env):
    """A JSON-array string for `streams` succeeds and matches the native list call."""
    payload = [
        {"type": "watts", "data": [100, 110, 120]},
        {"type": "heartrate", "data": [140, 142, 145]},
    ]
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.get("/activity/i999/streams").mock(return_value=Response(200, json=payload))

        string_response = await _call(
            "get_activity_streams",
            {"activity_id": "i999", "streams": '["watts","heartrate"]'},
        )
        native_response = await _call(
            "get_activity_streams",
            {"activity_id": "i999", "streams": ["watts", "heartrate"]},
        )

    assert "error" not in string_response, string_response
    assert "error" not in native_response, native_response
    assert string_response["data"]["streams"] == {
        "watts": [100, 110, 120],
        "heartrate": [140, 142, 145],
    }
    assert string_response["data"] == native_response["data"]


async def test_native_typed_calls_still_valid(configured_env):
    """Regression guard: native int/list args are unaffected by the widened schema."""
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.route(host="intervals.icu").mock(return_value=Response(200, json=[]))

        response = await _call("get_recent_activities", {"days_back": 1, "limit": 3})

    assert "error" not in response, response


# ---------------------------------------------------------------------------
# Global widening: params NOT annotated per-param must also accept string ints.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "string_args", "native_args"),
    [
        ("search_activities", {"query": "ride", "limit": "5"}, {"query": "ride", "limit": 5}),
        ("get_upcoming_workouts", {"limit": "5"}, {"limit": 5}),
        (
            "search_activities_full",
            {"query": "ride", "limit": "5"},
            {"query": "ride", "limit": 5},
        ),
    ],
)
async def test_globally_widened_tools_accept_string_ints(
    configured_env, tool_name, string_args, native_args
):
    """Tools widened by the global pass (no per-param annotation) accept string ints.

    Reaching the handler at all proves the string passed the strict pre-dispatch
    jsonschema gate; matching the native call proves it was coerced identically.
    """
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.route(host="intervals.icu").mock(return_value=Response(200, json=[]))

        string_response = await _call(tool_name, string_args)
        native_response = await _call(tool_name, native_args)

    assert string_response["data"] == native_response["data"]


async def test_update_wellness_accepts_string_int_and_float(configured_env):
    """A write tool with many numeric params accepts string ints/floats via widening."""
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.route(host="intervals.icu").mock(return_value=Response(200, json={"id": "2026-07-03"}))

        string_response = await _call(
            "update_wellness",
            {"date": "2026-07-03", "resting_hr": "48", "weight": "72.5"},
        )
        native_response = await _call(
            "update_wellness",
            {"date": "2026-07-03", "resting_hr": 48, "weight": 72.5},
        )

    assert "error" not in string_response, string_response
    assert string_response["data"] == native_response["data"]


class TestWidenPropertySchema:
    """Unit tests for the schema-widening transform."""

    def test_required_int_becomes_anyof_string(self):
        from intervals_icu_mcp.coercion import _widen_property_schema

        prop = {"type": "integer", "description": "Event ID"}
        assert _widen_property_schema(prop) is True
        assert prop == {
            "anyOf": [{"type": "integer"}, {"type": "string"}],
            "description": "Event ID",
        }

    def test_optional_int_appends_string_branch(self):
        from intervals_icu_mcp.coercion import _widen_property_schema

        prop = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}
        assert _widen_property_schema(prop) is True
        assert {"type": "string"} in prop["anyOf"]
        assert {"type": "null"} in prop["anyOf"]

    def test_number_and_array_are_widened(self):
        from intervals_icu_mcp.coercion import _widen_property_schema

        num = {"type": "number"}
        arr = {"type": "array", "items": {"type": "string"}}
        assert _widen_property_schema(num) is True
        assert _widen_property_schema(arr) is True
        # array constraints (items) are preserved inside the original branch
        assert {"type": "array", "items": {"type": "string"}} in arr["anyOf"]

    def test_string_and_boolean_untouched(self):
        from intervals_icu_mcp.coercion import _widen_property_schema

        for prop in ({"type": "string"}, {"type": "boolean"}):
            snapshot = dict(prop)
            assert _widen_property_schema(prop) is False
            assert prop == snapshot

    def test_idempotent(self):
        from intervals_icu_mcp.coercion import _widen_property_schema

        prop = {"type": "integer", "description": "x"}
        assert _widen_property_schema(prop) is True
        assert _widen_property_schema(prop) is False  # already has a string branch


async def test_no_bare_numeric_or_array_params_remain():
    """Every registered tool param is string-tolerant after the startup widening."""
    tools = await mcp.get_tools()
    bare: list[str] = []
    for name, tool in tools.items():
        for pname, p in (tool.parameters or {}).get("properties", {}).items():
            if pname == "ctx":
                continue
            if "anyOf" in p:
                kinds = {s.get("type") for s in p["anyOf"] if isinstance(s, dict)}
                widened = "string" in kinds
            else:
                kinds = {p.get("type")}
                widened = False
            if (kinds & {"integer", "number", "array"}) and not widened:
                bare.append(f"{name}.{pname}")
    assert bare == [], f"bare numeric/array params still present: {bare}"
