"""Tests for legacy field-name aliases on create_event / update_event.

Older claude.ai project instructions documented tool parameters by their
underlying API field names — `type` (for `event_type`) and `start_date_local`
(for `start_date`) — so Claude may call the tools with those keys and hit
"Unexpected keyword argument". Both tools now accept the legacy names as
validation aliases.

The aliases are resolved by FastMCP's argument-model validation, so these tests
drive the tools through the in-memory Client (a plain function call would raise a
Python TypeError for the unknown kwarg). Note the asymmetry: an *optional* param
takes the alias directly, but a *required* param (create_event.start_date) is
declared optional-in-schema and enforced in the body, because the strict
jsonschema `required` check runs before pydantic resolves the alias.
"""

import json
from collections.abc import Callable

import pytest
import respx
from fastmcp import Client
from httpx import Request, Response
from mcp.types import TextContent

import intervals_icu_mcp.server as server_module

mcp = server_module.mcp


@pytest.fixture
def configured_env(monkeypatch):
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")


def _echo_event(captured: dict) -> Callable[[Request], Response]:
    """Capture the outgoing API body and echo it back as a created/updated event."""

    def _handler(request: Request) -> Response:
        body = json.loads(request.content)
        captured["body"] = body
        return Response(
            200,
            json={
                "id": 42,
                "start_date_local": body.get("start_date_local", "2026-07-02"),
                "category": body.get("category", "WORKOUT"),
                "name": body.get("name", "Event"),
                "type": body.get("type"),
            },
        )

    return _handler


async def _call_tool(tool_name: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(tool_name, args)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return json.loads(block.text)


# ---------------- create_event: event_type <- type ----------------


async def test_create_event_accepts_type_alias(configured_env):
    """create_event(type="Ride") maps to event_type and is sent to the API as `type`."""
    captured: dict = {}
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.post("/athlete/i999/events").mock(side_effect=_echo_event(captured))
        response = await _call_tool(
            "create_event",
            {
                "start_date": "2026-07-02",
                "name": "Threshold",
                "category": "WORKOUT",
                "type": "Ride",  # legacy alias, NOT event_type
            },
        )

    assert "error" not in response, response
    assert captured["body"]["type"] == "Ride"
    assert response["data"]["type"] == "Ride"


async def test_create_event_still_accepts_event_type(configured_env):
    """The canonical `event_type` parameter still works unchanged."""
    captured: dict = {}
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.post("/athlete/i999/events").mock(side_effect=_echo_event(captured))
        response = await _call_tool(
            "create_event",
            {
                "start_date": "2026-07-02",
                "name": "Threshold",
                "category": "WORKOUT",
                "event_type": "Run",
            },
        )

    assert "error" not in response, response
    assert captured["body"]["type"] == "Run"


# ---------------- create_event: start_date <- start_date_local ----------------


async def test_create_event_accepts_start_date_local_alias(configured_env):
    """create_event(start_date_local=...) (API field name) maps to start_date."""
    captured: dict = {}
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.post("/athlete/i999/events").mock(side_effect=_echo_event(captured))
        response = await _call_tool(
            "create_event",
            {
                "start_date_local": "2026-07-02",  # legacy alias, NOT start_date
                "name": "Rest day reminder",
                "category": "NOTE",
            },
        )

    assert "error" not in response, response
    assert captured["body"]["start_date_local"].startswith("2026-07-02")


async def test_create_event_accepts_canonical_start_date(configured_env):
    """The canonical `start_date` parameter still works unchanged."""
    captured: dict = {}
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.post("/athlete/i999/events").mock(side_effect=_echo_event(captured))
        response = await _call_tool(
            "create_event",
            {"start_date": "2026-07-02", "name": "Note", "category": "NOTE"},
        )

    assert "error" not in response, response
    assert captured["body"]["start_date_local"].startswith("2026-07-02")


async def test_create_event_missing_start_date_errors(configured_env):
    """Omitting both start_date and its alias returns a clear validation error."""
    # No HTTP mock: the body guard returns before any API call.
    response = await _call_tool("create_event", {"name": "X", "category": "NOTE"})

    assert response["error"]["type"] == "validation_error", response
    assert "start_date is required" in response["error"]["message"]


# ---------------- update_event: symmetric aliases ----------------


async def test_update_event_accepts_legacy_aliases(configured_env):
    """update_event accepts start_date_local and type aliases (symmetric to create)."""
    captured: dict = {}

    def _handler(request: Request) -> Response:
        captured["body"] = json.loads(request.content)
        return Response(
            200,
            json={
                "id": 7,
                "start_date_local": captured["body"].get("start_date_local", "2026-07-02"),
                "category": "WORKOUT",
                "name": "Updated",
                "type": captured["body"].get("type"),
            },
        )

    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.put("/athlete/i999/events/7").mock(side_effect=_handler)
        response = await _call_tool(
            "update_event",
            {"event_id": 7, "start_date_local": "2026-07-03", "type": "Run"},
        )

    assert "error" not in response, response
    assert captured["body"]["start_date_local"].startswith("2026-07-03")
    assert captured["body"]["type"] == "Run"
