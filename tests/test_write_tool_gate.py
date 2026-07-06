"""Tests for the destructive-tool gate (R2, SEC-2).

The five delete tools plus ``apply_sport_settings`` must register only when
``ENABLE_WRITE_TOOLS`` is set. Registration happens at import time in
``server.py``, so these tests reload the module under each flag state (and
restore the default state afterwards so other test modules see the stock
server).
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from types import ModuleType

import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import Response

import intervals_icu_mcp.server as server_module

GATED: set[str] = set(server_module.GATED_DESTRUCTIVE_TOOLS)

ALWAYS_ON_WRITE_TOOLS = {
    "create_event",
    "update_event",
    "bulk_create_events",
    "duplicate_event",
    "mark_event_done",
    "update_activity",
    "create_gear",
    "update_gear",
    "create_gear_reminder",
    "update_gear_reminder",
    "update_wellness",
    "update_sport_settings",
    "create_sport_settings",
}


@pytest.fixture
def reload_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str | None], ModuleType]]:
    """Reload server.py with ENABLE_WRITE_TOOLS set to the given value (None = unset)."""

    def _reload(flag: str | None) -> ModuleType:
        if flag is None:
            monkeypatch.delenv("ENABLE_WRITE_TOOLS", raising=False)
        else:
            monkeypatch.setenv("ENABLE_WRITE_TOOLS", flag)
        return importlib.reload(server_module)

    yield _reload
    # Restore the default (flag unset) module state for the rest of the suite.
    os.environ.pop("ENABLE_WRITE_TOOLS", None)
    importlib.reload(server_module)


async def _tool_names(module: ModuleType) -> set[str]:
    async with Client(module.mcp) as client:
        return {tool.name for tool in await client.list_tools()}


class TestGateOff:
    async def test_gated_tools_absent_by_default(self, reload_server):
        module = reload_server(None)
        tools = await _tool_names(module)
        assert tools.isdisjoint(GATED), f"gated tools leaked: {tools & GATED}"

    async def test_everyday_write_tools_remain_registered(self, reload_server):
        module = reload_server(None)
        tools = await _tool_names(module)
        missing = ALWAYS_ON_WRITE_TOOLS - tools
        assert not missing, f"non-gated write tools missing: {missing}"

    async def test_calling_a_gated_tool_is_not_found_not_executed(self, reload_server, monkeypatch):
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")
        module = reload_server(None)
        with respx.mock(assert_all_called=False) as rx:
            upstream = rx.route(host="intervals.icu").mock(return_value=Response(200, json={}))
            async with Client(module.mcp) as client:
                with pytest.raises(ToolError, match="delete_event"):
                    await client.call_tool("delete_event", {"event_id": 123})
            assert not upstream.called, "gated tool must not reach the upstream API"

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    async def test_non_truthy_values_keep_gate_closed(self, reload_server, value):
        module = reload_server(value)
        tools = await _tool_names(module)
        assert tools.isdisjoint(GATED)


class TestGateOn:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
    async def test_gated_tools_register_when_enabled(self, reload_server, value):
        module = reload_server(value)
        tools = await _tool_names(module)
        missing = GATED - tools
        assert not missing, f"gated tools missing with flag={value!r}: {missing}"

    async def test_gated_tool_functions_when_enabled(self, reload_server, monkeypatch):
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")
        module = reload_server("true")
        with respx.mock(assert_all_called=False) as rx:
            rx.route(host="intervals.icu").mock(return_value=Response(200, json={}))
            async with Client(module.mcp) as client:
                result = await client.call_tool("delete_event", {"event_id": 123})
        assert result is not None
