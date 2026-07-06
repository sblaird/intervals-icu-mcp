"""Activity-related tools for Intervals.icu MCP server."""

import base64
import os
import tempfile
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastmcp import Context
from pydantic import WithJsonSchema

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient, dropped_items_metadata
from ..coercion import CoerceInt, int_schema
from ..response_builder import ResponseBuilder
from ..subjective_scales import (
    FEEL_SCALE_NOTE,
    parse_feel_label,
)
from ..subjective_scales import (
    feel_label as _format_feel_label,
)

# R4 (STB-H2): a season-long GPX can be 10-50 MB (+33% as base64); inlining it
# blows the MCP message limit and Cloud Run's 512 MiB budget. Files above this
# size must go to the scratch dir via output_path instead.
DOWNLOAD_MAX_INLINE_BYTES = 5 * 1024 * 1024


def _download_scratch_dir() -> str:
    """The only directory download tools may write to (env-overridable)."""
    return os.path.realpath(
        os.getenv("DOWNLOAD_SCRATCH_DIR")
        or os.path.join(tempfile.gettempdir(), "intervals-icu-mcp-downloads")
    )


def _resolve_output_path(output_path: str) -> str:
    """Resolve output_path inside the scratch dir; raise ValueError if it escapes.

    output_path must be relative — it is joined onto the scratch dir, and the
    resolved result must stay inside it (blocks absolute paths, drive letters,
    and ``..`` traversal). Creates parent directories as needed.
    """
    scratch_dir = _download_scratch_dir()
    if os.path.isabs(output_path) or os.path.splitdrive(output_path)[0]:
        raise ValueError(
            "output_path must be a relative path; files are saved inside the "
            f"scratch directory ({scratch_dir})"
        )
    candidate = os.path.realpath(os.path.join(scratch_dir, output_path))
    if os.path.commonpath([scratch_dir, candidate]) != scratch_dir:
        raise ValueError(
            "output_path may not escape the scratch directory "
            f"({scratch_dir}); remove '..' segments"
        )
    os.makedirs(os.path.dirname(candidate), exist_ok=True)
    return candidate


def _build_download_response(
    *,
    activity_id: str,
    file_content: bytes,
    output_path: str | None,
    query_type: str,
    format_label: str | None,
) -> str:
    """Shared save-or-inline handling for the three download tools (R4)."""
    format_data: dict[str, Any] = {"format": format_label} if format_label else {}
    label = format_label or "activity"

    if output_path:
        try:
            resolved = _resolve_output_path(output_path)
        except ValueError as exc:
            return ResponseBuilder.build_error_response(str(exc), error_type="validation_error")
        with open(resolved, "wb") as f:
            f.write(file_content)
        return ResponseBuilder.build_response(
            data={
                "activity_id": activity_id,
                **format_data,
                "saved_to": resolved,
                "size_bytes": len(file_content),
            },
            query_type=query_type,
            metadata={
                "message": f"{label} file saved to {resolved}",
                "bytes": len(file_content),
                "encoding": "file",
            },
        )

    if len(file_content) > DOWNLOAD_MAX_INLINE_BYTES:
        return ResponseBuilder.build_error_response(
            f"File is {len(file_content)} bytes, above the "
            f"{DOWNLOAD_MAX_INLINE_BYTES}-byte inline limit. Call again with "
            "output_path (a relative filename) to save it to the scratch "
            "directory instead of returning the bytes.",
            error_type="validation_error",
        )

    encoded = base64.b64encode(file_content).decode("utf-8")
    return ResponseBuilder.build_response(
        data={
            "activity_id": activity_id,
            **format_data,
            "size_bytes": len(file_content),
            "content_base64": encoded,
            "note": f"File content is base64 encoded. Decode to get {label} file.",
        },
        query_type=query_type,
        metadata={"bytes": len(file_content), "encoding": "base64"},
    )


async def get_recent_activities(
    limit: Annotated[
        int, CoerceInt, WithJsonSchema(int_schema("Number of activities to fetch"))
    ] = 30,
    days_back: Annotated[
        int, CoerceInt, WithJsonSchema(int_schema("Number of days to look back"))
    ] = 30,
    ctx: Context | None = None,
) -> str:
    """Get recent activities for the authenticated athlete.

    Returns a summary of recent activities including key metrics like distance,
    duration, power, heart rate, and training load.

    Args:
        limit: Number of activities to fetch (default 30, max 100)
        days_back: Number of days to look back (default 30)

    Returns:
        JSON string with activity summaries
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        # Calculate date range
        oldest_date = datetime.now() - timedelta(days=days_back)
        oldest = oldest_date.strftime("%Y-%m-%d")

        async with ICUClient(config) as client:
            activities, dropped = await client.get_activities(
                oldest=oldest,
                limit=min(limit, 100),  # Cap at 100
            )
            dropped_meta = dropped_items_metadata(dropped, label="activity")

            if not activities:
                return ResponseBuilder.build_response(
                    data={"activities": [], "count": 0},
                    metadata={"message": "No activities found", **dropped_meta},
                )

            activities_data: list[dict[str, Any]] = []
            for activity in activities:
                activity_item: dict[str, Any] = {
                    "id": activity.id,
                    "name": activity.name or "Untitled",
                    "start_date": activity.start_date_local,
                    "type": activity.type,
                }

                if activity.distance:
                    activity_item["distance_meters"] = activity.distance

                if activity.moving_time:
                    activity_item["moving_time_seconds"] = activity.moving_time

                if activity.total_elevation_gain:
                    activity_item["elevation_gain_meters"] = activity.total_elevation_gain

                # Performance metrics
                if activity.average_watts:
                    activity_item["average_watts"] = activity.average_watts
                if activity.normalized_power:
                    activity_item["normalized_power"] = activity.normalized_power
                if activity.average_heartrate:
                    activity_item["average_heartrate"] = activity.average_heartrate
                if activity.average_cadence:
                    activity_item["average_cadence"] = activity.average_cadence

                # Training load
                if activity.icu_training_load:
                    activity_item["training_load"] = activity.icu_training_load
                if activity.icu_intensity:
                    activity_item["intensity_factor"] = activity.icu_intensity

                activities_data.append(activity_item)

            return ResponseBuilder.build_response(
                data={"activities": activities_data, "count": len(activities_data)},
                query_type="recent_activities",
                metadata=dropped_meta or None,
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def get_activity_details(
    activity_id: Annotated[str, "Activity ID to fetch"],
    ctx: Context | None = None,
) -> str:
    """Get detailed information for a specific activity.

    Returns comprehensive activity details including all metrics, weather,
    and performance data.

    Args:
        activity_id: The unique ID of the activity

    Returns:
        JSON string with detailed activity information
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            activity = await client.get_activity(activity_id=activity_id)

            activity_data: dict[str, Any] = {
                "id": activity.id,
                "name": activity.name or "Untitled",
                "type": activity.type,
                "start_date": activity.start_date_local,
            }

            if activity.description:
                activity_data["description"] = activity.description

            # Duration and distance
            if activity.moving_time:
                activity_data["moving_time_seconds"] = activity.moving_time
            if activity.elapsed_time:
                activity_data["elapsed_time_seconds"] = activity.elapsed_time
            if activity.distance:
                activity_data["distance_meters"] = activity.distance
            if activity.total_elevation_gain:
                activity_data["elevation_gain_meters"] = activity.total_elevation_gain

            # Speed/Pace
            if activity.average_speed:
                activity_data["average_speed_meters_per_sec"] = activity.average_speed
            if activity.max_speed:
                activity_data["max_speed_meters_per_sec"] = activity.max_speed

            # Power metrics
            power_metrics: dict[str, Any] = {}
            if activity.average_watts:
                power_metrics["average"] = activity.average_watts
            if activity.normalized_power:
                power_metrics["normalized"] = activity.normalized_power
            if activity.weighted_average_watts:
                power_metrics["weighted_average"] = activity.weighted_average_watts
            if activity.max_watts:
                power_metrics["max"] = activity.max_watts
            if activity.variability_index:
                power_metrics["variability_index"] = round(activity.variability_index, 2)
            if activity.efficiency_factor:
                power_metrics["efficiency_factor"] = round(activity.efficiency_factor, 2)
            if power_metrics:
                activity_data["power"] = power_metrics

            # Heart rate
            hr_metrics: dict[str, Any] = {}
            if activity.average_heartrate:
                hr_metrics["average"] = activity.average_heartrate
            if activity.max_heartrate:
                hr_metrics["max"] = activity.max_heartrate
            if hr_metrics:
                activity_data["heart_rate"] = hr_metrics

            # Cadence
            cadence_metrics: dict[str, Any] = {}
            if activity.average_cadence:
                cadence_metrics["average"] = activity.average_cadence
            if activity.max_cadence:
                cadence_metrics["max"] = activity.max_cadence
            if cadence_metrics:
                activity_data["cadence"] = cadence_metrics

            # Training load
            training_metrics: dict[str, Any] = {}
            if activity.icu_training_load:
                training_metrics["training_load"] = activity.icu_training_load
            if activity.icu_intensity:
                training_metrics["intensity_factor"] = activity.icu_intensity
            if activity.tss:
                training_metrics["tss"] = round(activity.tss, 0)
            if activity.hrss:
                training_metrics["hrss"] = round(activity.hrss, 0)
            if activity.trimp:
                training_metrics["trimp"] = round(activity.trimp, 0)
            if activity.decoupling is not None:
                training_metrics["decoupling_percent"] = round(activity.decoupling, 2)
            if activity.polarization_index is not None:
                training_metrics["polarization_index"] = round(activity.polarization_index, 2)
            if activity.session_rpe:
                training_metrics["session_rpe"] = activity.session_rpe
            if activity.strain_score is not None:
                training_metrics["strain_score"] = round(activity.strain_score, 1)
            if activity.power_load:
                training_metrics["power_load"] = activity.power_load
            if activity.hr_load:
                training_metrics["hr_load"] = activity.hr_load
            if activity.pace_load:
                training_metrics["pace_load"] = activity.pace_load
            if training_metrics:
                activity_data["training"] = training_metrics

            # Zone-time distribution (R14)
            zone_times: dict[str, Any] = {}
            if activity.icu_zone_times:
                zone_times["power"] = activity.icu_zone_times
            if activity.icu_hr_zone_times:
                zone_times["heart_rate"] = activity.icu_hr_zone_times
            if activity.pace_zone_times:
                zone_times["pace"] = activity.pace_zone_times
            if activity.gap_zone_times:
                zone_times["gap"] = activity.gap_zone_times
            if zone_times:
                zone_times["note"] = "Seconds spent per zone (power entries carry zone ids)."
                activity_data["zone_times"] = zone_times

            # Fitness / power model at ride time (R14)
            fitness: dict[str, Any] = {}
            if activity.icu_ctl is not None:
                fitness["ctl"] = round(activity.icu_ctl, 1)
            if activity.icu_atl is not None:
                fitness["atl"] = round(activity.icu_atl, 1)
            if activity.icu_rolling_ftp:
                fitness["rolling_ftp"] = activity.icu_rolling_ftp
            if activity.icu_rolling_ftp_delta:
                fitness["rolling_ftp_delta"] = activity.icu_rolling_ftp_delta
            if activity.icu_pm_ftp:
                fitness["power_model_ftp"] = activity.icu_pm_ftp
            if activity.icu_pm_cp:
                fitness["power_model_cp"] = activity.icu_pm_cp
            if activity.icu_pm_w_prime:
                fitness["power_model_w_prime_joules"] = activity.icu_pm_w_prime
            if fitness:
                activity_data["fitness"] = fitness

            # Fueling / energy (R14)
            fueling: dict[str, Any] = {}
            if activity.icu_joules:
                fueling["work_joules"] = activity.icu_joules
            if activity.icu_joules_above_ftp:
                fueling["work_above_ftp_joules"] = activity.icu_joules_above_ftp
            if activity.carbs_used:
                fueling["carbs_used_grams"] = activity.carbs_used
            if activity.carbs_ingested:
                fueling["carbs_ingested_grams"] = activity.carbs_ingested
            if fueling:
                activity_data["fueling"] = fueling

            # Environment (R14)
            environment: dict[str, Any] = {}
            if activity.average_temp is not None:
                environment["average_temp_c"] = activity.average_temp
            if activity.average_wind_speed is not None:
                environment["average_wind_speed"] = activity.average_wind_speed
            if activity.headwind_percent is not None:
                environment["headwind_percent"] = activity.headwind_percent
            if activity.tailwind_percent is not None:
                environment["tailwind_percent"] = activity.tailwind_percent
            if environment:
                activity_data["environment"] = environment

            # Subjective metrics. ``feel`` is on intervals.icu's inverted
            # 1-5 scale (1=best, 5=worst); always emit a label so downstream
            # consumers can't misread the integer as Garmin's display value.
            subjective: dict[str, Any] = {}
            if activity.feel:
                subjective["feel"] = activity.feel
                label = _format_feel_label(activity.feel)
                if label:
                    subjective["feel_label"] = label
                subjective["feel_scale_note"] = FEEL_SCALE_NOTE
            if activity.perceived_exertion:
                subjective["rpe"] = activity.perceived_exertion
            if subjective:
                activity_data["subjective"] = subjective

            # Other info
            other_info: dict[str, Any] = {}
            if activity.calories:
                other_info["calories"] = activity.calories
            if activity.device_name:
                other_info["device"] = activity.device_name
            if activity.trainer or activity.indoor:
                other_info["indoor"] = True
            if activity.commute:
                other_info["commute"] = True
            if activity.race:
                other_info["race"] = True
            if activity.tags:
                other_info["tags"] = activity.tags
            if activity.gap is not None:
                other_info["gap_meters_per_sec"] = activity.gap
            if activity.coasting_time:
                other_info["coasting_time_seconds"] = activity.coasting_time
            if activity.interval_summary:
                other_info["interval_summary"] = activity.interval_summary
            if other_info:
                activity_data["other"] = other_info

            return ResponseBuilder.build_response(
                data=activity_data,
                query_type="activity_details",
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def search_activities(
    query: Annotated[str, "Search query (activity name or tag)"],
    limit: Annotated[int, "Maximum number of results to return"] = 30,
    ctx: Context | None = None,
) -> str:
    """Search for activities by name or tag.

    Searches the athlete's activity history for matching activities based on
    name or tags. Useful for finding specific workouts or activity types.

    Args:
        query: Search term (e.g., "threshold", "long run", "race")
        limit: Maximum number of results (default 30)

    Returns:
        JSON string with matching activities
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    if not query.strip():
        return ResponseBuilder.build_error_response(
            "Search query cannot be empty",
            error_type="validation_error",
        )

    try:
        async with ICUClient(config) as client:
            results, dropped = await client.search_activities(
                query=query,
                limit=min(limit, 100),  # Cap at 100
            )
            dropped_meta = dropped_items_metadata(dropped, label="activity search result")

            if not results:
                return ResponseBuilder.build_response(
                    data={"activities": [], "count": 0, "query": query},
                    metadata={"message": f"No activities found matching '{query}'", **dropped_meta},
                )

            activities_data: list[dict[str, Any]] = []
            for result in results:
                activity_item: dict[str, Any] = {
                    "id": result.id,
                    "name": result.name or "Untitled",
                    "start_date": result.start_date_local,
                    "type": result.type,
                }

                if result.distance:
                    activity_item["distance_meters"] = result.distance

                if result.moving_time:
                    activity_item["moving_time_seconds"] = result.moving_time

                activities_data.append(activity_item)

            return ResponseBuilder.build_response(
                data={"activities": activities_data, "count": len(activities_data), "query": query},
                query_type="search_activities",
                metadata=dropped_meta or None,
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def update_activity(
    activity_id: Annotated[str, "Activity ID to update"],
    name: Annotated[str | None, "Updated activity name"] = None,
    description: Annotated[str | None, "Updated description"] = None,
    activity_type: Annotated[str | None, "Updated activity type (e.g., Ride, Run, Swim)"] = None,
    trainer: Annotated[bool | None, "Mark as trainer/indoor workout"] = None,
    commute: Annotated[bool | None, "Mark as commute"] = None,
    feel: Annotated[
        int | None,
        "How you felt on intervals.icu's 1-5 scale: 1=Very Strong (best), "
        "5=Very Weak (worst). Note: this is INVERSE of Garmin's display scale.",
    ] = None,
    feel_label: Annotated[
        str | None,
        "Direction-safe alternative to `feel`. Accepts 'very_strong', 'strong', "
        "'normal', 'weak', 'very_weak' (case-insensitive). Mapped to the correct "
        "intervals.icu integer internally. If both `feel` and `feel_label` are "
        "given, `feel_label` wins.",
    ] = None,
    perceived_exertion: Annotated[int | None, "RPE rating (1-10 scale)"] = None,
    ctx: Context | None = None,
) -> str:
    """Update an existing activity's metadata.

    Modifies one or more fields of an existing activity. Only provide the fields
    you want to change - other fields will remain unchanged.

    Args:
        activity_id: The unique ID of the activity to update
        name: New name for the activity
        description: New description/notes for the activity
        activity_type: New activity type (e.g., "Ride", "Run", "Swim")
        trainer: Whether this was an indoor trainer workout
        commute: Whether this was a commute
        feel: Subjective feel rating on intervals.icu's scale — 1=Very Strong
            (best) through 5=Very Weak (worst). Inverse of Garmin's display.
            Prefer `feel_label` to avoid direction confusion.
        feel_label: Label string ('very_strong' .. 'very_weak'). Wins over `feel`.
        perceived_exertion: RPE rating (1-10 scale)

    Returns:
        JSON string with updated activity information
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        # Build update data (only include provided fields)
        activity_data: dict[str, Any] = {}

        if name is not None:
            activity_data["name"] = name
        if description is not None:
            activity_data["description"] = description
        if activity_type is not None:
            activity_data["type"] = activity_type
        if trainer is not None:
            activity_data["trainer"] = trainer
        if commute is not None:
            activity_data["commute"] = commute
        # Label wins over int so callers who pass both don't get a silent
        # direction mismatch.
        if feel_label is not None:
            mapped = parse_feel_label(feel_label)
            if mapped is None:
                return ResponseBuilder.build_error_response(
                    f"Unknown feel_label '{feel_label}'. Accepted: "
                    "very_strong, strong, normal, weak, very_weak.",
                    error_type="validation_error",
                )
            activity_data["feel"] = mapped
        elif feel is not None:
            if not 1 <= feel <= 5:
                return ResponseBuilder.build_error_response(
                    f"feel must be 1-5 (1=Very Strong, 5=Very Weak); got {feel}.",
                    error_type="validation_error",
                )
            activity_data["feel"] = feel
        if perceived_exertion is not None:
            activity_data["perceived_exertion"] = perceived_exertion

        if not activity_data:
            return ResponseBuilder.build_error_response(
                "No fields provided to update. Please specify at least one field to change.",
                error_type="validation_error",
            )

        async with ICUClient(config) as client:
            activity = await client.update_activity(activity_id, activity_data)

            result_data: dict[str, Any] = {
                "id": activity.id,
                "name": activity.name or "Untitled",
                "type": activity.type,
                "start_date": activity.start_date_local,
            }

            if activity.description:
                result_data["description"] = activity.description
            if activity.trainer is not None:
                result_data["trainer"] = activity.trainer
            if activity.commute is not None:
                result_data["commute"] = activity.commute
            if activity.feel is not None:
                result_data["feel"] = activity.feel
                label = _format_feel_label(activity.feel)
                if label:
                    result_data["feel_label"] = label
                result_data["feel_scale_note"] = FEEL_SCALE_NOTE
            if activity.perceived_exertion is not None:
                result_data["rpe"] = activity.perceived_exertion

            return ResponseBuilder.build_response(
                data=result_data,
                query_type="update_activity",
                metadata={"message": f"Successfully updated activity {activity_id}"},
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def delete_activity(
    activity_id: Annotated[str, "Activity ID to delete"],
    ctx: Context | None = None,
) -> str:
    """Delete an activity permanently.

    Permanently removes an activity from your account. This action cannot be undone.
    Use with caution.

    Args:
        activity_id: The unique ID of the activity to delete

    Returns:
        JSON string with deletion confirmation
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            success = await client.delete_activity(activity_id)

            if success:
                return ResponseBuilder.build_response(
                    data={"activity_id": activity_id, "deleted": True},
                    query_type="delete_activity",
                    metadata={"message": f"Successfully deleted activity {activity_id}"},
                )
            else:
                return ResponseBuilder.build_error_response(
                    f"Failed to delete activity {activity_id}",
                    error_type="api_error",
                )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def download_activity_file(
    activity_id: Annotated[str, "Activity ID to download"],
    output_path: Annotated[
        str | None,
        "Optional relative filename; the file is saved inside the server's "
        "scratch directory. Omit to get base64 content inline (small files only).",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Download the original activity file.

    Downloads the original file that was uploaded to Intervals.icu (FIT, TCX, or GPX).
    Files above the inline limit (~5 MB) must be saved via output_path.

    Args:
        activity_id: The unique ID of the activity
        output_path: Optional relative filename (e.g., "activity.fit"), saved
            inside the server's scratch directory

    Returns:
        JSON string with file info and base64-encoded content (if no output_path)
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            file_content = await client.download_activity_file(activity_id)
            return _build_download_response(
                activity_id=activity_id,
                file_content=file_content,
                output_path=output_path,
                query_type="download_activity_file",
                format_label=None,
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def download_fit_file(
    activity_id: Annotated[str, "Activity ID to download"],
    output_path: Annotated[
        str | None,
        "Optional relative filename; the file is saved inside the server's "
        "scratch directory. Omit to get base64 content inline (small files only).",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Download activity as a FIT file.

    Converts and downloads the activity as a FIT (Flexible and Interoperable Data Transfer)
    file, which is compatible with Garmin and most training platforms.

    Args:
        activity_id: The unique ID of the activity
        output_path: Optional relative filename (e.g., "activity.fit"), saved
            inside the server's scratch directory

    Returns:
        JSON string with file info and base64-encoded content (if no output_path)
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            file_content = await client.download_fit_file(activity_id)
            return _build_download_response(
                activity_id=activity_id,
                file_content=file_content,
                output_path=output_path,
                query_type="download_fit_file",
                format_label="FIT",
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def download_gpx_file(
    activity_id: Annotated[str, "Activity ID to download"],
    output_path: Annotated[
        str | None,
        "Optional relative filename; the file is saved inside the server's "
        "scratch directory. Omit to get base64 content inline (small files only).",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Download activity as a GPX file.

    Converts and downloads the activity as a GPX (GPS Exchange Format) file,
    which is compatible with most GPS devices and mapping software.

    Args:
        activity_id: The unique ID of the activity
        output_path: Optional relative filename (e.g., "activity.gpx"), saved
            inside the server's scratch directory

    Returns:
        JSON string with file info and base64-encoded content (if no output_path)
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            file_content = await client.download_gpx_file(activity_id)
            return _build_download_response(
                activity_id=activity_id,
                file_content=file_content,
                output_path=output_path,
                query_type="download_gpx_file",
                format_label="GPX",
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def search_activities_full(
    query: Annotated[str, "Search query (activity name or tag)"],
    limit: Annotated[int, "Maximum number of results to return"] = 30,
    ctx: Context | None = None,
) -> str:
    """Search for activities by name or tag, returning complete activity details.

    Unlike the basic search, this returns full Activity objects with all metrics,
    power data, heart rate, training load, and more. Use this when you need
    detailed information about matching activities.

    Args:
        query: Search term (e.g., "threshold", "long run", "race", "#interval")
        limit: Maximum number of results (default 30)

    Returns:
        JSON string with complete activity details for matches
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    if not query.strip():
        return ResponseBuilder.build_error_response(
            "Search query cannot be empty",
            error_type="validation_error",
        )

    try:
        async with ICUClient(config) as client:
            activities, dropped = await client.search_activities_full(
                query=query,
                limit=min(limit, 100),
            )
            dropped_meta = dropped_items_metadata(dropped, label="activity")

            if not activities:
                return ResponseBuilder.build_response(
                    data={"activities": [], "count": 0, "query": query},
                    metadata={"message": f"No activities found matching '{query}'", **dropped_meta},
                )

            activities_data: list[dict[str, Any]] = []
            for activity in activities:
                activity_item: dict[str, Any] = {
                    "id": activity.id,
                    "name": activity.name or "Untitled",
                    "type": activity.type,
                    "start_date": activity.start_date_local,
                }

                # Basic metrics
                if activity.distance:
                    activity_item["distance_meters"] = activity.distance
                if activity.moving_time:
                    activity_item["moving_time_seconds"] = activity.moving_time
                if activity.total_elevation_gain:
                    activity_item["elevation_gain_meters"] = activity.total_elevation_gain

                # Performance metrics
                performance: dict[str, Any] = {}
                if activity.average_watts:
                    performance["average_watts"] = activity.average_watts
                if activity.normalized_power:
                    performance["normalized_power"] = activity.normalized_power
                if activity.average_heartrate:
                    performance["average_heartrate"] = activity.average_heartrate
                if activity.average_cadence:
                    performance["average_cadence"] = activity.average_cadence
                if performance:
                    activity_item["performance"] = performance

                # Training load
                if activity.icu_training_load:
                    activity_item["training_load"] = activity.icu_training_load
                if activity.icu_intensity:
                    activity_item["intensity_factor"] = activity.icu_intensity

                activities_data.append(activity_item)

            return ResponseBuilder.build_response(
                data={
                    "activities": activities_data,
                    "count": len(activities_data),
                    "query": query,
                },
                query_type="search_activities_full",
                metadata=dropped_meta or None,
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def get_activities_around(
    activity_id: Annotated[str, "Reference activity ID"],
    count: Annotated[int, "Number of activities before and after"] = 5,
    ctx: Context | None = None,
) -> str:
    """Get activities before and after a specific activity for context.

    Retrieves activities chronologically surrounding a reference activity.
    Useful for understanding training context, progression, or finding
    related workouts.

    Args:
        activity_id: The ID of the reference activity
        count: Number of activities to retrieve before and after (default 5)

    Returns:
        JSON string with activities around the reference activity
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            activities, dropped = await client.get_activities_around(
                activity_id=activity_id,
                count=count,
            )
            dropped_meta = dropped_items_metadata(dropped, label="activity")

            if not activities:
                return ResponseBuilder.build_response(
                    data={
                        "activities": [],
                        "count": 0,
                        "reference_activity_id": activity_id,
                    },
                    metadata={
                        "message": "No activities found around the reference activity",
                        **dropped_meta,
                    },
                )

            # Sort by date. start_date_local is Optional here; give the key a
            # total order so a null date can't raise TypeError (None sorts last,
            # and None-vs-None / naive-vs-aware datetimes are never compared).
            activities.sort(
                key=lambda x: (x.start_date_local is None, x.start_date_local or datetime.min)
            )

            # Find the reference activity position
            ref_index = next((i for i, a in enumerate(activities) if a.id == activity_id), None)

            activities_data: list[dict[str, Any]] = []
            for i, activity in enumerate(activities):
                activity_item: dict[str, Any] = {
                    "id": activity.id,
                    "name": activity.name or "Untitled",
                    "type": activity.type,
                    "start_date": activity.start_date_local,
                }

                # Mark if this is the reference activity
                if activity.id == activity_id:
                    activity_item["is_reference"] = True
                elif ref_index is not None:
                    if i < ref_index:
                        activity_item["position"] = "before"
                        activity_item["days_before"] = ref_index - i
                    else:
                        activity_item["position"] = "after"
                        activity_item["days_after"] = i - ref_index

                # Basic metrics
                if activity.distance:
                    activity_item["distance_meters"] = activity.distance
                if activity.moving_time:
                    activity_item["moving_time_seconds"] = activity.moving_time
                if activity.icu_training_load:
                    activity_item["training_load"] = activity.icu_training_load

                # Performance summary
                performance: dict[str, Any] = {}
                if activity.average_watts:
                    performance["average_watts"] = activity.average_watts
                if activity.average_heartrate:
                    performance["average_heartrate"] = activity.average_heartrate
                if performance:
                    activity_item["performance"] = performance

                activities_data.append(activity_item)

            result_data = {
                "reference_activity_id": activity_id,
                "activities": activities_data,
                "count": len(activities_data),
            }

            if ref_index is not None:
                result_data["reference_position"] = ref_index
                result_data["activities_before"] = ref_index
                result_data["activities_after"] = len(activities) - ref_index - 1

            return ResponseBuilder.build_response(
                data=result_data,
                query_type="activities_around",
                metadata=dropped_meta or None,
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )
