"""Event/calendar management tools for Intervals.icu MCP server."""

import re
from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..response_builder import ResponseBuilder

_DATETIME_HELP = (
    "Date 'YYYY-MM-DD' (treated as 00:00 local) or ISO-8601 datetime "
    "'YYYY-MM-DDTHH:MM:SS' (timezone offset is stripped — the API field is local)"
)

_DESCRIPTION_HELP = (
    "Workout description in Intervals.icu syntax. Steps inside repeat blocks "
    "MUST be prefixed with '- ', e.g.:\n"
    "  2x\n  - 20m 220-235w\n  - 10m 150-180w\n"
    "Without the '- ' prefix the upstream parser silently drops the repeat "
    "structure (and `duration_seconds`/`training_load` will be wrong). "
    "Free-text descriptions are fine for notes."
)

_LOAD_OVERRIDE_NOTE = (
    " Note: Intervals.icu's server recomputes this from the description when "
    "the description is structured (parses to steps); honored when description "
    "is empty or free-text."
)


_REPEAT_HEADER_RE = re.compile(r"^\s*\d+x\s*$", re.IGNORECASE)
_DASH_PREFIX_RE = re.compile(r"^\s*-\s+")
_LEADING_WS_RE = re.compile(r"^(\s*)")


def _expand_repeat_blocks(description: str) -> str:
    """Normalize Nx repeat blocks for Intervals.icu's parser.

    Why: Intervals.icu's parser requires '- ' prefixes on each step inside a
    repeat block AND fails when blank lines separate the header from steps
    or steps from each other. Without normalization, the block falls back to
    free-text and moving_time/training_load come out empty (the '2x bug'
    from the user's bug report). Verified against the live API on
    2026-05-07: dashes alone weren't enough — the blank line still produced
    moving_time=1800 instead of 3600 until we also dropped intra-block
    blanks.

    Algorithm: when we see a line matching `^\\d+x$`, treat the following
    non-blank lines as steps — DROP blank lines (they break parsing), prefix
    with '- ' if missing, and exit the block at the first non-numeric line
    (presumed section header like 'Cool down'). Lines already prefixed are
    left alone (idempotent).
    """
    lines = description.split("\n")
    out: list[str] = []
    in_repeat = False
    for line in lines:
        stripped = line.strip()
        if _REPEAT_HEADER_RE.match(stripped):
            in_repeat = True
            out.append(line)
            continue
        if not in_repeat:
            out.append(line)
            continue
        # Inside a repeat block.
        if not stripped:
            # Drop blank lines — they break the parser's block detection.
            continue
        if _DASH_PREFIX_RE.match(line):
            out.append(line)
            continue
        # Numeric content → looks like a step (e.g. "20m 220w", "10min 80% FTP").
        if re.search(r"\d", stripped):
            indent_match = _LEADING_WS_RE.match(line)
            indent = indent_match.group(1) if indent_match else ""
            out.append(f"{indent}- {stripped}")
            continue
        # Non-numeric text (e.g. "Cool down") → exit the repeat block, leave as-is.
        in_repeat = False
        out.append(line)
    return "\n".join(out)


def _normalize_event_datetime(value: str) -> str:
    """Normalize a date or datetime string for the Intervals.icu events API.

    Why: the upstream API rejects bare 'YYYY-MM-DD' (Java parser fails at index 10)
    and requires a full ISO-8601 datetime for `start_date_local`. We accept either
    form from callers and emit what the API wants.

    Accepts 'YYYY-MM-DD' (padded to T00:00:00) or any value parseable by
    `datetime.fromisoformat` (timezone is dropped because the field is local).
    Raises ValueError on anything else.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError) as e:
        raise ValueError(
            "Invalid date format. Use 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS' (ISO-8601 datetime)."
        ) from e
    return parsed.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


async def create_event(
    start_date: Annotated[str, f"Start date or datetime. {_DATETIME_HELP}"],
    name: Annotated[str, "Event name"],
    category: Annotated[str, "Event category: WORKOUT, NOTE, RACE, or GOAL"],
    description: Annotated[str | None, f"Optional. {_DESCRIPTION_HELP}"] = None,
    event_type: Annotated[str | None, "Activity type (e.g., Ride, Run, Swim)"] = None,
    duration_seconds: Annotated[
        int | None, f"Planned duration in seconds.{_LOAD_OVERRIDE_NOTE}"
    ] = None,
    distance_meters: Annotated[float | None, "Planned distance in meters"] = None,
    training_load: Annotated[int | None, f"Planned training load.{_LOAD_OVERRIDE_NOTE}"] = None,
    ctx: Context | None = None,
) -> str:
    """Create a new calendar event (planned workout, note, race, or goal).

    Adds an event to your Intervals.icu calendar. Events can be workouts with
    planned metrics, notes for tracking information, races, or training goals.

    Args:
        start_date: Date 'YYYY-MM-DD' or ISO-8601 datetime 'YYYY-MM-DDTHH:MM:SS'
        name: Name of the event
        category: Type of event - WORKOUT, NOTE, RACE, or GOAL
        description: Optional Intervals.icu workout syntax. Steps inside repeat
            blocks must be '- '-prefixed; this server will auto-prefix any
            unmarked steps it detects inside `Nx` blocks.
        event_type: Activity type (e.g., "Ride", "Run", "Swim") for workouts
        duration_seconds: Planned duration. Overridden by upstream parser when
            description is structured.
        distance_meters: Planned distance in meters
        training_load: Planned training load. Overridden by upstream parser when
            description is structured.

    Returns:
        JSON string with created event data
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    # Validate category
    valid_categories = ["WORKOUT", "NOTE", "RACE", "GOAL"]
    if category.upper() not in valid_categories:
        return ResponseBuilder.build_error_response(
            f"Invalid category. Must be one of: {', '.join(valid_categories)}",
            error_type="validation_error",
        )

    try:
        normalized_start = _normalize_event_datetime(start_date)
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    try:
        # Build event data
        event_data: dict[str, Any] = {
            "start_date_local": normalized_start,
            "name": name,
            "category": category.upper(),
        }

        if description:
            event_data["description"] = _expand_repeat_blocks(description)
        if event_type:
            event_data["type"] = event_type
        if duration_seconds:
            event_data["moving_time"] = duration_seconds
        if distance_meters:
            event_data["distance"] = distance_meters
        if training_load:
            event_data["icu_training_load"] = training_load

        async with ICUClient(config) as client:
            event = await client.create_event(event_data)

            event_result: dict[str, Any] = {
                "id": event.id,
                "start_date": event.start_date_local,
                "name": event.name,
                "category": event.category,
            }

            if event.description:
                event_result["description"] = event.description
            if event.type:
                event_result["type"] = event.type
            if event.moving_time:
                event_result["duration_seconds"] = event.moving_time
            if event.distance:
                event_result["distance_meters"] = event.distance
            if event.icu_training_load:
                event_result["training_load"] = event.icu_training_load

            return ResponseBuilder.build_response(
                data=event_result,
                query_type="create_event",
                metadata={"message": f"Successfully created {category.lower()}: {name}"},
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def update_event(
    event_id: Annotated[int, "Event ID to update"],
    name: Annotated[str | None, "Updated event name"] = None,
    description: Annotated[str | None, f"Updated description. {_DESCRIPTION_HELP}"] = None,
    start_date: Annotated[str | None, f"Updated start date or datetime. {_DATETIME_HELP}"] = None,
    event_type: Annotated[str | None, "Updated activity type"] = None,
    duration_seconds: Annotated[
        int | None, f"Updated duration in seconds.{_LOAD_OVERRIDE_NOTE}"
    ] = None,
    distance_meters: Annotated[float | None, "Updated distance in meters"] = None,
    training_load: Annotated[int | None, f"Updated training load.{_LOAD_OVERRIDE_NOTE}"] = None,
    ctx: Context | None = None,
) -> str:
    """Update an existing calendar event.

    Modifies one or more fields of an existing event. Only provide the fields
    you want to change - other fields will remain unchanged.

    Args:
        event_id: ID of the event to update
        name: New name for the event
        description: New description
        start_date: New start date 'YYYY-MM-DD' or ISO-8601 datetime
        event_type: New activity type
        duration_seconds: New planned duration
        distance_meters: New planned distance
        training_load: New planned training load

    Returns:
        JSON string with updated event data
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    normalized_start: str | None = None
    if start_date is not None:
        try:
            normalized_start = _normalize_event_datetime(start_date)
        except ValueError as e:
            return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    try:
        # Build update data (only include provided fields)
        event_data: dict[str, Any] = {}

        if name is not None:
            event_data["name"] = name
        if description is not None:
            event_data["description"] = _expand_repeat_blocks(description)
        if normalized_start is not None:
            event_data["start_date_local"] = normalized_start
        if event_type is not None:
            event_data["type"] = event_type
        if duration_seconds is not None:
            event_data["moving_time"] = duration_seconds
        if distance_meters is not None:
            event_data["distance"] = distance_meters
        if training_load is not None:
            event_data["icu_training_load"] = training_load

        if not event_data:
            return ResponseBuilder.build_error_response(
                "No fields provided to update. Please specify at least one field to change.",
                error_type="validation_error",
            )

        async with ICUClient(config) as client:
            event = await client.update_event(event_id, event_data)

            event_result: dict[str, Any] = {
                "id": event.id,
                "start_date": event.start_date_local,
                "name": event.name,
                "category": event.category,
            }

            if event.description:
                event_result["description"] = event.description
            if event.type:
                event_result["type"] = event.type
            if event.moving_time:
                event_result["duration_seconds"] = event.moving_time
            if event.distance:
                event_result["distance_meters"] = event.distance
            if event.icu_training_load:
                event_result["training_load"] = event.icu_training_load

            return ResponseBuilder.build_response(
                data=event_result,
                query_type="update_event",
                metadata={"message": f"Successfully updated event {event_id}"},
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def delete_event(
    event_id: Annotated[int, "Event ID to delete"],
    ctx: Context | None = None,
) -> str:
    """Delete a calendar event.

    Permanently removes an event from your calendar. This action cannot be undone.

    Args:
        event_id: ID of the event to delete

    Returns:
        JSON string with deletion confirmation
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            success = await client.delete_event(event_id)

            if success:
                return ResponseBuilder.build_response(
                    data={"event_id": event_id, "deleted": True},
                    query_type="delete_event",
                    metadata={"message": f"Successfully deleted event {event_id}"},
                )
            else:
                return ResponseBuilder.build_error_response(
                    f"Failed to delete event {event_id}",
                    error_type="api_error",
                )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def bulk_create_events(
    events: Annotated[
        str,
        (
            "JSON string containing array of events. Each event should have: "
            "start_date_local, name, category, and optional fields like "
            "description, type, moving_time, distance, icu_training_load. "
            "Description follows Intervals.icu workout syntax — see create_event "
            "for the dash-prefix requirement on repeat-block steps."
        ),
    ],
    ctx: Context | None = None,
) -> str:
    """Create multiple calendar events in a single operation.

    This is more efficient than creating events one at a time. Provide a JSON array
    of event objects, each with the same structure as create_event.

    Note: descriptions are auto-passed through `_expand_repeat_blocks` so unmarked
    steps inside Nx blocks get '- ' prefixes. moving_time/icu_training_load are
    overridden by Intervals.icu when the description parses to structured steps.

    Args:
        events: JSON array of event objects to create

    Returns:
        JSON string with created events
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        import json

        # Parse the JSON string
        try:
            parsed_data = json.loads(events)
        except json.JSONDecodeError as e:
            return ResponseBuilder.build_error_response(
                f"Invalid JSON format: {str(e)}", error_type="validation_error"
            )

        if not isinstance(parsed_data, list):
            return ResponseBuilder.build_error_response(
                "Events must be a JSON array", error_type="validation_error"
            )

        # Type cast after validation
        events_data: list[dict[str, Any]] = parsed_data  # type: ignore[assignment]

        # Validate each event
        valid_categories = ["WORKOUT", "NOTE", "RACE", "GOAL"]
        for i, event_data in enumerate(events_data):
            if "start_date_local" not in event_data:
                return ResponseBuilder.build_error_response(
                    f"Event {i}: Missing required field 'start_date_local'",
                    error_type="validation_error",
                )
            if "name" not in event_data:
                return ResponseBuilder.build_error_response(
                    f"Event {i}: Missing required field 'name'", error_type="validation_error"
                )
            if "category" not in event_data:
                return ResponseBuilder.build_error_response(
                    f"Event {i}: Missing required field 'category'",
                    error_type="validation_error",
                )
            if event_data["category"].upper() not in valid_categories:
                return ResponseBuilder.build_error_response(
                    f"Event {i}: Invalid category. Must be one of: {', '.join(valid_categories)}",
                    error_type="validation_error",
                )

            # Normalize category to uppercase
            event_data["category"] = event_data["category"].upper()

            # Normalize start_date_local to ISO-8601 datetime the API accepts
            try:
                event_data["start_date_local"] = _normalize_event_datetime(
                    event_data["start_date_local"]
                )
            except ValueError as e:
                return ResponseBuilder.build_error_response(
                    f"Event {i}: {e}", error_type="validation_error"
                )

            # Auto-prefix unmarked steps inside Nx repeat blocks so the upstream
            # parser doesn't silently drop the structure.
            if isinstance(event_data.get("description"), str):
                event_data["description"] = _expand_repeat_blocks(event_data["description"])

        async with ICUClient(config) as client:
            created_events = await client.bulk_create_events(events_data)

            events_result: list[dict[str, Any]] = []
            for event in created_events:
                event_info: dict[str, Any] = {
                    "id": event.id,
                    "start_date": event.start_date_local,
                    "name": event.name,
                    "category": event.category,
                }

                if event.description:
                    event_info["description"] = event.description
                if event.type:
                    event_info["type"] = event.type
                if event.moving_time:
                    event_info["duration_seconds"] = event.moving_time
                if event.distance:
                    event_info["distance_meters"] = event.distance
                if event.icu_training_load:
                    event_info["training_load"] = event.icu_training_load

                events_result.append(event_info)

            return ResponseBuilder.build_response(
                data={"events": events_result},
                query_type="bulk_create_events",
                metadata={
                    "message": f"Successfully created {len(created_events)} events",
                    "count": len(created_events),
                },
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def bulk_delete_events(
    event_ids: Annotated[str, "JSON array of event IDs to delete (e.g., '[123, 456, 789]')"],
    ctx: Context | None = None,
) -> str:
    """Delete multiple calendar events in a single operation.

    This is more efficient than deleting events one at a time. Provide a JSON array
    of event IDs to delete.

    Args:
        event_ids: JSON array of event IDs (integers)

    Returns:
        JSON string with deletion confirmation
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        import json

        # Parse the JSON string
        try:
            parsed_data = json.loads(event_ids)
        except json.JSONDecodeError as e:
            return ResponseBuilder.build_error_response(
                f"Invalid JSON format: {str(e)}", error_type="validation_error"
            )

        if not isinstance(parsed_data, list):
            return ResponseBuilder.build_error_response(
                "Event IDs must be a JSON array", error_type="validation_error"
            )

        if not parsed_data:
            return ResponseBuilder.build_error_response(
                "Must provide at least one event ID to delete", error_type="validation_error"
            )

        # Type cast after validation
        ids_list: list[int] = parsed_data  # type: ignore[assignment]

        async with ICUClient(config) as client:
            result = await client.bulk_delete_events(ids_list)

            return ResponseBuilder.build_response(
                data={"deleted_count": len(ids_list), "event_ids": ids_list, "result": result},
                query_type="bulk_delete_events",
                metadata={"message": f"Successfully deleted {len(ids_list)} events"},
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def duplicate_event(
    event_id: Annotated[int, "Event ID to duplicate"],
    new_date: Annotated[str, f"New date or datetime for the duplicated event. {_DATETIME_HELP}"],
    ctx: Context | None = None,
) -> str:
    """Duplicate an existing event to a new date.

    Creates a copy of an event with all its properties (name, type, duration, etc.)
    but with a new date. Useful for repeating workouts or events.

    Args:
        event_id: ID of the event to duplicate
        new_date: New date 'YYYY-MM-DD' or ISO-8601 datetime

    Returns:
        JSON string with the duplicated event
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        normalized_new_date = _normalize_event_datetime(new_date)
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    try:
        async with ICUClient(config) as client:
            duplicated_event = await client.duplicate_event(event_id, normalized_new_date)

            event_result: dict[str, Any] = {
                "id": duplicated_event.id,
                "start_date": duplicated_event.start_date_local,
                "name": duplicated_event.name,
                "category": duplicated_event.category,
                "original_event_id": event_id,
            }

            if duplicated_event.description:
                event_result["description"] = duplicated_event.description
            if duplicated_event.type:
                event_result["type"] = duplicated_event.type
            if duplicated_event.moving_time:
                event_result["duration_seconds"] = duplicated_event.moving_time
            if duplicated_event.distance:
                event_result["distance_meters"] = duplicated_event.distance
            if duplicated_event.icu_training_load:
                event_result["training_load"] = duplicated_event.icu_training_load

            return ResponseBuilder.build_response(
                data=event_result,
                query_type="duplicate_event",
                metadata={
                    "message": f"Successfully duplicated event {event_id} to {new_date}",
                    "original_event_id": event_id,
                },
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )
