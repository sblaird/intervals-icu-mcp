"""Tests for retry/backoff on transient upstream failures (R8, STB-M3).

_request used to be single-shot: 429/5xx and connection errors surfaced
immediately as tool failures, prompting the LLM to re-issue whole call
chains. Now transient statuses (429/500/502/503/504) and httpx.RequestError
get up to 2 jittered retries, honouring Retry-After on 429; other 4xx and
successes are never retried.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from httpx import Response

from intervals_icu_mcp.client import ICUAPIError, ICUClient


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Zero out real sleeping; capture requested delays for assertions."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def instant_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    return delays


async def _get(respx_mock, mock_config, route_responses) -> tuple[httpx.Response, Any]:
    route = respx_mock.get("/athlete/i123456/gear").mock(side_effect=route_responses)
    async with ICUClient(mock_config) as client:
        response = await client._request("GET", "/athlete/i123456/gear")
    return response, route


class TestRetryableFailures:
    async def test_503_then_200_succeeds(self, mock_config, respx_mock):
        response, route = await _get(
            respx_mock, mock_config, [Response(503), Response(200, json=[])]
        )
        assert response.status_code == 200
        assert route.call_count == 2

    async def test_connect_error_then_200_succeeds(self, mock_config, respx_mock):
        response, route = await _get(
            respx_mock,
            mock_config,
            [httpx.ConnectError("boom"), Response(200, json=[])],
        )
        assert response.status_code == 200
        assert route.call_count == 2

    async def test_persistent_500_fails_after_retry_budget(self, mock_config, respx_mock):
        route = respx_mock.get("/athlete/i123456/gear").mock(return_value=Response(500))
        async with ICUClient(mock_config) as client:
            with pytest.raises(ICUAPIError) as excinfo:
                await client._request("GET", "/athlete/i123456/gear")
        assert excinfo.value.status_code == 500
        assert route.call_count == ICUClient.MAX_RETRIES + 1

    async def test_persistent_connect_error_fails_after_retry_budget(self, mock_config, respx_mock):
        route = respx_mock.get("/athlete/i123456/gear").mock(side_effect=httpx.ConnectError("down"))
        async with ICUClient(mock_config) as client:
            with pytest.raises(ICUAPIError, match="Request failed"):
                await client._request("GET", "/athlete/i123456/gear")
        assert route.call_count == ICUClient.MAX_RETRIES + 1

    async def test_retry_after_header_honoured(self, mock_config, respx_mock, fast_retries):
        respx_mock.get("/athlete/i123456/gear").mock(
            side_effect=[
                Response(429, headers={"Retry-After": "3"}),
                Response(200, json=[]),
            ]
        )
        async with ICUClient(mock_config) as client:
            response = await client._request("GET", "/athlete/i123456/gear")
        assert response.status_code == 200
        assert fast_retries == [3.0]

    async def test_retry_after_is_capped(self, mock_config, respx_mock, fast_retries):
        respx_mock.get("/athlete/i123456/gear").mock(
            side_effect=[
                Response(429, headers={"Retry-After": "9999"}),
                Response(200, json=[]),
            ]
        )
        async with ICUClient(mock_config) as client:
            await client._request("GET", "/athlete/i123456/gear")
        assert fast_retries == [ICUClient.RETRY_MAX_DELAY_SECONDS]

    async def test_exhausted_429_returns_rate_limit_error(self, mock_config, respx_mock):
        route = respx_mock.get("/athlete/i123456/gear").mock(return_value=Response(429))
        async with ICUClient(mock_config) as client:
            with pytest.raises(ICUAPIError, match="Rate limit"):
                await client._request("GET", "/athlete/i123456/gear")
        assert route.call_count == ICUClient.MAX_RETRIES + 1


class TestErrorBodyNotReflected:
    """R9 (SEC-4): upstream error bodies are logged, never returned."""

    async def test_error_body_absent_from_message_but_logged(self, mock_config, respx_mock, caplog):
        import logging

        secret_body = "stacktrace: internal-database-hostname-and-query"
        respx_mock.get("/athlete/i123456/gear").mock(return_value=Response(422, text=secret_body))
        with caplog.at_level(logging.WARNING, logger="intervals_icu_mcp.client"):
            async with ICUClient(mock_config) as client:
                with pytest.raises(ICUAPIError) as excinfo:
                    await client._request("GET", "/athlete/i123456/gear")

        assert secret_body not in excinfo.value.message
        assert excinfo.value.message == "intervals.icu returned HTTP 422 for this request."
        assert excinfo.value.status_code == 422
        assert any(secret_body in record.getMessage() for record in caplog.records)


class TestNonRetryable:
    async def test_404_is_not_retried(self, mock_config, respx_mock):
        route = respx_mock.get("/athlete/i123456/gear").mock(return_value=Response(404))
        async with ICUClient(mock_config) as client:
            with pytest.raises(ICUAPIError, match="not found"):
                await client._request("GET", "/athlete/i123456/gear")
        assert route.call_count == 1

    async def test_401_is_not_retried(self, mock_config, respx_mock):
        route = respx_mock.get("/athlete/i123456/gear").mock(return_value=Response(401))
        async with ICUClient(mock_config) as client:
            with pytest.raises(ICUAPIError, match="Unauthorized"):
                await client._request("GET", "/athlete/i123456/gear")
        assert route.call_count == 1

    async def test_success_makes_exactly_one_request(self, mock_config, respx_mock):
        route = respx_mock.get("/athlete/i123456/gear").mock(return_value=Response(200, json=[]))
        async with ICUClient(mock_config) as client:
            response = await client._request("GET", "/athlete/i123456/gear")
        assert response.status_code == 200
        assert route.call_count == 1
