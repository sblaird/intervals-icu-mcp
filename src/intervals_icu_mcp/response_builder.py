"""Response builder utilities for structured JSON output.

This module provides utilities for building consistent, structured JSON responses
across all MCP tools. All tools return JSON with a standard structure:

{
    "data": {...},           # Main data payload
    "analysis": {...},       # Optional insights and computed metrics
    "metadata": {...}        # Query metadata, timestamps, includes
}
"""

import json
import logging
import sys
from datetime import datetime
from typing import Any, cast

logger = logging.getLogger(__name__)


def _convert_datetimes(obj: Any) -> Any:  # type: ignore[misc]
    """Recursively convert datetime objects to ISO strings."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {str(k): _convert_datetimes(v) for k, v in obj.items()}  # type: ignore[misc]
    elif isinstance(obj, list):
        return [_convert_datetimes(item) for item in obj]  # type: ignore[misc]
    return obj


class ResponseBuilder:
    """Builder for standardized JSON responses."""

    @staticmethod
    def format_date_with_day(dt: datetime | str | None) -> dict[str, str] | None:
        """Format a date/datetime with explicit day-of-week information.

        Args:
            dt: datetime object or ISO string or None

        Returns:
            Dict with datetime, date, day_of_week, and formatted string, or None if input is None
        """
        if dt is None:
            return None

        # Parse the datetime if it's a string, otherwise use it directly.
        # R12 (STB-L2): an unparseable upstream date must not crash the whole
        # tool response — return the raw value instead.
        if isinstance(dt, str):
            try:
                parsed_dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except ValueError:
                return {"datetime": dt}
        else:
            parsed_dt = dt

        return {
            "datetime": dt if isinstance(dt, str) else dt.isoformat(),
            "date": parsed_dt.strftime("%Y-%m-%d"),
            "day_of_week": parsed_dt.strftime("%A"),  # e.g., "Monday"
            "formatted": parsed_dt.strftime(
                "%A, %B %d, %Y at %I:%M %p"
            ),  # e.g., "Monday, October 15, 2025 at 02:30 PM"
        }

    @staticmethod
    def build_response(
        data: dict[str, Any],
        analysis: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        query_type: str | None = None,
    ) -> str:
        """Build standardized JSON response.

        Args:
            data: Main data payload
            analysis: Optional analysis and insights
            metadata: Optional metadata (will be enriched with timestamp)
            query_type: Optional query type for metadata

        Returns:
            JSON string with structure:
            {
                "data": {...},
                "analysis": {...},
                "metadata": {
                    "fetched_at": "ISO timestamp",
                    "query_type": "...",
                    ...
                }
            }
        """
        # Convert datetime objects to ISO strings
        converted_data = cast(dict[str, Any], _convert_datetimes(data))
        converted_analysis: dict[str, Any] | None = None
        if analysis:
            converted_analysis = cast(dict[str, Any], _convert_datetimes(analysis))

        response: dict[str, Any] = {"data": converted_data}

        if converted_analysis:
            response["analysis"] = converted_analysis

        # Build metadata with timestamp
        meta = metadata or {}
        converted_meta = cast(dict[str, Any], _convert_datetimes(meta))
        converted_meta["fetched_at"] = datetime.now().isoformat()
        if query_type:
            converted_meta["query_type"] = query_type

        response["metadata"] = converted_meta

        return json.dumps(response, separators=(",", ":"))

    @staticmethod
    def build_error_response(
        error_message: str,
        error_type: str = "error",
        suggestions: list[str] | None = None,
    ) -> str:
        """Build standardized error response.

        Args:
            error_message: Human-readable error message
            error_type: Type of error (e.g., "not_found", "rate_limit", "validation")
            suggestions: Optional list of suggestions to resolve the error

        Returns:
            JSON string with error structure
        """
        # R13 (STB-L4): the ~59 blanket `except Exception` handlers all funnel
        # through here with internal/unexpected error types. Log the active
        # traceback centrally so unexpected failures are diagnosable from
        # Cloud Run logs (why the latlng bug was opaque), without touching
        # every handler or changing the returned shape.
        if error_type in ("internal_error", "unexpected_error") and sys.exc_info()[0] is not None:
            logger.exception("Tool handler failed unexpectedly: %s", error_message)

        response: dict[str, dict[str, str | list[str]]] = {
            "error": {
                "message": error_message,
                "type": error_type,
                "timestamp": datetime.now().isoformat(),
            }
        }

        if suggestions:
            response["error"]["suggestions"] = suggestions

        return json.dumps(response, separators=(",", ":"))
