"""Tests for download-tool payload and path guards (R4, STB-H2).

The three download tools used to base64 the entire file into the response
when output_path was omitted (a season GPX can be 10-50 MB, +33% as base64)
and wrote to arbitrary server paths. Now: files above the inline limit
require output_path, and output_path is constrained to a scratch directory
(no absolute paths, no ``..`` escapes).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import pytest
import respx
from fastmcp import Client
from httpx import Response
from mcp.types import TextContent

import intervals_icu_mcp.server as server_module
import intervals_icu_mcp.tools.activities as activities_module
from intervals_icu_mcp.tools.activities import _resolve_output_path

mcp = server_module.mcp

DOWNLOAD_TOOLS = ["download_activity_file", "download_fit_file", "download_gpx_file"]


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("DOWNLOAD_SCRATCH_DIR", str(scratch))
    return scratch


async def _call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(tool_name, args)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return json.loads(block.text)


def _mock_file(rx: respx.MockRouter, content: bytes) -> None:
    rx.route(host="intervals.icu").mock(return_value=Response(200, content=content))


class TestInlineLimit:
    @pytest.mark.parametrize("tool_name", DOWNLOAD_TOOLS)
    async def test_oversized_file_without_output_path_is_refused(
        self, configured_env, monkeypatch, tool_name
    ):
        monkeypatch.setattr(activities_module, "DOWNLOAD_MAX_INLINE_BYTES", 1000)
        with respx.mock(assert_all_called=False) as rx:
            _mock_file(rx, b"x" * 2000)
            payload = await _call(tool_name, {"activity_id": "i1"})

        assert payload["error"]["type"] == "validation_error"
        assert "output_path" in payload["error"]["message"]
        assert "content_base64" not in json.dumps(payload)

    @pytest.mark.parametrize("tool_name", DOWNLOAD_TOOLS)
    async def test_small_file_still_inlined(self, configured_env, tool_name):
        content = b"small fit file bytes"
        with respx.mock(assert_all_called=False) as rx:
            _mock_file(rx, content)
            payload = await _call(tool_name, {"activity_id": "i1"})

        assert base64.b64decode(payload["data"]["content_base64"]) == content
        assert payload["metadata"]["bytes"] == len(content)
        assert payload["metadata"]["encoding"] == "base64"

    async def test_oversized_file_with_output_path_is_saved(self, configured_env, monkeypatch):
        monkeypatch.setattr(activities_module, "DOWNLOAD_MAX_INLINE_BYTES", 1000)
        content = b"y" * 2000
        with respx.mock(assert_all_called=False) as rx:
            _mock_file(rx, content)
            payload = await _call(
                "download_gpx_file", {"activity_id": "i1", "output_path": "big.gpx"}
            )

        saved_to = payload["data"]["saved_to"]
        assert os.path.commonpath([str(configured_env), saved_to]) == str(configured_env)
        with open(saved_to, "rb") as f:
            assert f.read() == content
        assert payload["metadata"]["encoding"] == "file"
        assert payload["metadata"]["bytes"] == len(content)


class TestOutputPathConstraint:
    @pytest.mark.parametrize("bad_path", ["/abs/path.fit", "../escape.fit", "a/../../escape.fit"])
    async def test_escaping_paths_rejected(self, configured_env, bad_path):
        with respx.mock(assert_all_called=False) as rx:
            _mock_file(rx, b"data")
            payload = await _call(
                "download_fit_file", {"activity_id": "i1", "output_path": bad_path}
            )

        assert payload["error"]["type"] == "validation_error"
        # Nothing may be written outside the scratch dir; the scratch dir
        # itself should not contain an escaped file either.
        assert not os.path.exists(os.path.join(str(configured_env), os.pardir, "escape.fit"))

    async def test_relative_path_lands_in_scratch_dir(self, configured_env):
        content = b"fit bytes"
        with respx.mock(assert_all_called=False) as rx:
            _mock_file(rx, content)
            payload = await _call(
                "download_fit_file", {"activity_id": "i1", "output_path": "sub/dir/ride.fit"}
            )

        saved_to = payload["data"]["saved_to"]
        assert os.path.commonpath([str(configured_env), saved_to]) == str(configured_env)
        with open(saved_to, "rb") as f:
            assert f.read() == content

    def test_resolve_output_path_windows_drive_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOWNLOAD_SCRATCH_DIR", str(tmp_path))
        with pytest.raises(ValueError):
            _resolve_output_path("C:evil.fit")

    def test_resolve_output_path_plain_name_ok(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOWNLOAD_SCRATCH_DIR", str(tmp_path))
        resolved = _resolve_output_path("ride.fit")
        assert os.path.commonpath([os.path.realpath(str(tmp_path)), resolved]) == os.path.realpath(
            str(tmp_path)
        )
