"""Route tools for Intervals.icu MCP server.

Exposes the route catalog and route-vs-route similarity scoring. Useful for
"how did today's effort compare to last time on this loop?" questions —
route similarity narrows the population of comparable rides before any
power/HR analysis runs.
"""

from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..response_builder import ResponseBuilder


async def list_routes(
    ctx: Context | None = None,
) -> str:
    """List the athlete's saved routes with activity counts.

    The route path (latlngs) is intentionally NOT included to keep the
    payload small — fetch a specific route via `get_route` with
    `include_path=True` if you need the geometry.

    Returns:
        JSON string with route summaries.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            routes = await client.list_routes()
            return ResponseBuilder.build_response(
                data={"routes": routes, "count": len(routes)},
                query_type="list_routes",
            )
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def get_route(
    route_id: Annotated[int, "Route ID"],
    include_path: Annotated[
        bool,
        "If True, include the latlng path geometry (large payload). "
        "Leave False for metadata-only queries.",
    ] = False,
    ctx: Context | None = None,
) -> str:
    """Get a single route by ID.

    Args:
        route_id: Route ID
        include_path: Whether to include the latlng path geometry

    Returns:
        JSON string with the route details.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            route: dict[str, Any] = await client.get_route(route_id, include_path=include_path)
            return ResponseBuilder.build_response(
                data={"route": route},
                query_type="get_route",
                metadata={"include_path": include_path},
            )
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def compare_route_similarity(
    route_id: Annotated[int, "First route ID"],
    other_route_id: Annotated[int, "Second route ID to compare against"],
    include_paths: Annotated[
        bool,
        "If True, include both routes' latlng path geometry (large payload). "
        "Leave False for the similarity metrics only.",
    ] = False,
    ctx: Context | None = None,
) -> str:
    """Compute the similarity score between two of the athlete's routes.

    Use this to confirm two rides covered the same loop before comparing
    power/HR/duration. By default the routes' raw path arrays are omitted
    (same convention as `get_route`); pass include_paths=True for geometry.

    Args:
        route_id: First route ID
        other_route_id: Second route ID to compare against
        include_paths: Whether to include both routes' latlng path arrays

    Returns:
        JSON string with the similarity payload.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            similarity: dict[str, Any] = await client.get_route_similarity(
                route_id, other_route_id, include_paths=include_paths
            )
            return ResponseBuilder.build_response(
                data={
                    "route_id": route_id,
                    "other_route_id": other_route_id,
                    "similarity": similarity,
                },
                query_type="route_similarity",
                metadata={"include_paths": include_paths},
            )
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )
