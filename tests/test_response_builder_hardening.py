"""Tests for response-builder hardening (R12 STB-L2, R13 STB-L4)."""

from __future__ import annotations

import json
import logging

import respx
from fastmcp import Client
from httpx import Response
from mcp.types import TextContent

import intervals_icu_mcp.server as server_module
from intervals_icu_mcp.response_builder import ResponseBuilder

mcp = server_module.mcp


class TestFormatDateWithDay:
    def test_garbage_date_returns_raw_value(self):
        """R12: an unparseable upstream date must not raise."""
        result = ResponseBuilder.format_date_with_day("not-a-date-at-all")
        assert result == {"datetime": "not-a-date-at-all"}

    def test_valid_iso_string_still_formats(self):
        result = ResponseBuilder.format_date_with_day("2026-07-06T09:30:00")
        assert result is not None
        assert result["date"] == "2026-07-06"
        assert result["day_of_week"] == "Monday"

    def test_none_returns_none(self):
        assert ResponseBuilder.format_date_with_day(None) is None


class TestHandlerExceptionLogging:
    """R13: blanket except-Exception handlers must log the traceback."""

    def test_internal_error_with_active_exception_logs_traceback(self, caplog):
        with caplog.at_level(logging.ERROR, logger="intervals_icu_mcp.response_builder"):
            try:
                raise ValueError("kaboom")
            except Exception as e:
                payload = ResponseBuilder.build_error_response(
                    f"Unexpected error: {e}", error_type="internal_error"
                )

        body = json.loads(payload)
        assert body["error"]["type"] == "internal_error"
        records = [r for r in caplog.records if "Tool handler failed" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None  # traceback attached

    def test_plain_errors_do_not_log(self, caplog):
        with caplog.at_level(logging.ERROR, logger="intervals_icu_mcp.response_builder"):
            ResponseBuilder.build_error_response("bad input", error_type="validation_error")
            # internal_error without an active exception also stays quiet.
            ResponseBuilder.build_error_response("no exc", error_type="internal_error")
        assert not caplog.records

    async def test_induced_tool_exception_logged_and_structured(self, monkeypatch, caplog):
        """End-to-end: a handler crash is logged with traceback AND returns
        the structured error, unchanged."""
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", "test_api_key_12345")
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", "i999")

        with caplog.at_level(logging.ERROR, logger="intervals_icu_mcp.response_builder"):
            with respx.mock(assert_all_called=False) as rx:
                # Non-JSON body makes response.json() raise inside the handler.
                rx.route(host="intervals.icu").mock(
                    return_value=Response(200, text="<html>not json</html>")
                )
                async with Client(mcp) as client:
                    result = await client.call_tool("get_gear_list", {})
                    block = result.content[0]
                    assert isinstance(block, TextContent)
                    body = json.loads(block.text)

        assert body["error"]["type"] == "unexpected_error"
        assert any(
            "Tool handler failed" in r.getMessage() and r.exc_info is not None
            for r in caplog.records
        )
