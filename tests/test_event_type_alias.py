"""Tests for the `type` -> `event_type` alias on create_event (Issue #4 remainder).

Older claude.ai project instructions documented this parameter as `type`, so
Claude may call ``create_event(type="Ride")`` and hit "Unexpected keyword
argument [type]". The tool now accepts `type` as a validation alias for
`event_type`. The alias is resolved by FastMCP's argument-model validation, so
these tests must drive the tool through the in-memory Client (a plain function
call would raise a Python TypeError for the unknown `type` kwarg).
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


def _mock_create(captured: dict) -> Callable[[Request], Response]:
    def _handler(request: Request) -> Response:
        captured["body"] = json.loads(request.content)
        return Response(
            200,
            json={
                "id": 42,
                "start_date_local": "2026-07-02",
                "category": "WORKOUT",
                "name": "Threshold",
                "type": captured["body"].get("type"),
            },
        )

    return _handler


async def _create_event(args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool("create_event", args)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return json.loads(block.text)


async def test_create_event_accepts_type_alias(configured_env):
    """create_event(type="Ride") maps to event_type and is sent to the API as `type`."""
    captured: dict = {}
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.post("/athlete/i999/events").mock(side_effect=_mock_create(captured))
        response = await _create_event(
            {
                "start_date": "2026-07-02",
                "name": "Threshold",
                "category": "WORKOUT",
                "type": "Ride",  # legacy alias, NOT event_type
            }
        )

    assert "error" not in response, response
    # Alias resolved -> WORKOUT type-required check passed, API body carried `type`.
    assert captured["body"]["type"] == "Ride"
    assert response["data"]["type"] == "Ride"


async def test_create_event_still_accepts_event_type(configured_env):
    """The canonical `event_type` parameter still works unchanged."""
    captured: dict = {}
    with respx.mock(base_url="https://intervals.icu/api/v1", assert_all_called=False) as rx:
        rx.post("/athlete/i999/events").mock(side_effect=_mock_create(captured))
        response = await _create_event(
            {
                "start_date": "2026-07-02",
                "name": "Threshold",
                "category": "WORKOUT",
                "event_type": "Run",
            }
        )

    assert "error" not in response, response
    assert captured["body"]["type"] == "Run"
    assert response["data"]["type"] == "Run"
