"""Weather tools for Intervals.icu MCP server.

Exposes Intervals.icu's two weather endpoints — per-activity weather summary
(what it was actually like) and athlete forecast (what it will be like for
upcoming planned events). Useful for ride debriefs and race-week planning.
"""

from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..response_builder import ResponseBuilder


async def get_weather_forecast(
    ctx: Context | None = None,
) -> str:
    """Get the weather forecast for the athlete's upcoming planned events.

    Intervals.icu pulls forecast data for each scheduled event with a
    location. Useful for race-week or hot-day planning. Returns the raw
    forecast payload — keys vary by event location and forecast horizon.

    Returns:
        JSON string with the forecast payload.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            forecast = await client.get_weather_forecast()
            return ResponseBuilder.build_response(
                data={"forecast": forecast},
                query_type="weather_forecast",
            )
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def get_activity_weather(
    activity_id: Annotated[str, "Activity ID to fetch weather summary for"],
    start_index: Annotated[
        int | None, "Optional stream index start, for partial-window queries"
    ] = None,
    end_index: Annotated[
        int | None, "Optional stream index end, for partial-window queries"
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Get the weather summary recorded for a completed activity.

    Returns temperature, wind, humidity, and similar conditions captured
    during the ride. Single biggest unlock for "why was today harder?"
    analysis on outdoor activities.

    Args:
        activity_id: ID of the activity
        start_index: Optional start index into the activity's data stream
        end_index: Optional end index for the partial window

    Returns:
        JSON string with the activity weather summary.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            summary: dict[str, Any] = await client.get_activity_weather(
                activity_id, start_index=start_index, end_index=end_index
            )
            return ResponseBuilder.build_response(
                data={"activity_id": activity_id, "weather": summary},
                query_type="activity_weather",
            )
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )
