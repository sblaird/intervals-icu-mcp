"""Tests for the lean tool-set gate (LEAN_TOOLS).

claude.ai's chat surface appears to drop a custom connector whose tool count is
large (this server exposes 55 tools with writes off). ``LEAN_TOOLS`` registers
only a curated coaching core (``LEAN_CORE_TOOLS``) so the connector fits under
whatever budget the chat surface enforces. Registration happens at import time
in ``server.py``, so these tests reload the module under each flag state and
restore the default (flag unset) afterwards.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from types import ModuleType

import pytest
import respx
from fastmcp import Client
from httpx import Response

import intervals_icu_mcp.server as server_module

LEAN_CORE: set[str] = set(server_module.LEAN_CORE_TOOLS)


@pytest.fixture
def reload_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str | None], ModuleType]]:
    """Reload server.py with LEAN_TOOLS set to the given value (None = unset)."""

    def _reload(flag: str | None) -> ModuleType:
        if flag is None:
            monkeypatch.delenv("LEAN_TOOLS", raising=False)
        else:
            monkeypatch.setenv("LEAN_TOOLS", flag)
        return importlib.reload(server_module)

    yield _reload
    # Restore the default (flag unset) module state for the rest of the suite.
    os.environ.pop("LEAN_TOOLS", None)
    importlib.reload(server_module)


async def _tool_names(module: ModuleType) -> set[str]:
    async with Client(module.mcp) as client:
        return {tool.name for tool in await client.list_tools()}


class TestLeanGateOff:
    async def test_full_set_registered_by_default(self, reload_server):
        module = reload_server(None)
        tools = await _tool_names(module)
        # The full connector exposes far more than the lean core.
        assert len(tools) > len(LEAN_CORE)
        # Every lean tool is part of the full set (lean is a strict subset).
        missing = LEAN_CORE - tools
        assert not missing, f"lean tools missing from full set: {missing}"

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    async def test_non_truthy_values_keep_full_set(self, reload_server, value):
        module = reload_server(value)
        tools = await _tool_names(module)
        assert len(tools) > len(LEAN_CORE)


class TestLeanGateOn:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
    async def test_only_lean_tools_register_when_enabled(self, reload_server, value):
        module = reload_server(value)
        tools = await _tool_names(module)
        assert tools == LEAN_CORE, (
            f"unexpected extras: {tools - LEAN_CORE}; missing: {LEAN_CORE - tools}"
        )

    async def test_lean_core_is_small(self):
        # Guardrail: keep the lean set small so it stays under the chat budget.
        assert len(LEAN_CORE) <= 20

    async def test_a_lean_tool_functions_when_enabled(self, reload_server, monkeypatch):
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")
        module = reload_server("true")
        with respx.mock(assert_all_called=False) as rx:
            rx.route(host="intervals.icu").mock(
                return_value=Response(200, json={"id": "i999", "name": "Test"})
            )
            async with Client(module.mcp) as client:
                result = await client.call_tool("get_athlete_profile", {})
        assert result is not None
