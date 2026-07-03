"""Sport-specific settings tools for FTP, FTHR, pace thresholds, and zones."""

from typing import Annotated, Any

from fastmcp import Context

from ..auth import load_config, validate_credentials
from ..client import ICUAPIError, ICUClient
from ..models import SportSettings
from ..response_builder import ResponseBuilder


def _serialize_sport_settings(settings: SportSettings) -> dict[str, Any]:
    """Flatten a SportSettings into an LLM-friendly dict of thresholds + zones.

    Only populated fields are included so a swim/run entry (no FTP) doesn't carry
    empty power keys and vice versa. threshold_pace is passed through raw with its
    pace_units (see model note) rather than converted, since intervals.icu's stored
    value is not reliably min:sec.
    """
    info: dict[str, Any] = {"id": settings.id, "types": settings.types}

    # Power
    if settings.ftp is not None:
        info["ftp_watts"] = settings.ftp
    if settings.indoor_ftp is not None:
        info["indoor_ftp_watts"] = settings.indoor_ftp
    if settings.w_prime is not None:
        info["w_prime_joules"] = settings.w_prime
    if settings.p_max is not None:
        info["p_max_watts"] = settings.p_max
    if settings.power_zones:
        info["power_zones"] = settings.power_zones
    if settings.power_zone_names:
        info["power_zone_names"] = settings.power_zone_names
    if settings.sweet_spot_min is not None:
        info["sweet_spot_min"] = settings.sweet_spot_min
    if settings.sweet_spot_max is not None:
        info["sweet_spot_max"] = settings.sweet_spot_max

    # Heart rate
    if settings.lthr is not None:
        info["lthr_bpm"] = settings.lthr
    if settings.max_hr is not None:
        info["max_hr_bpm"] = settings.max_hr
    if settings.hr_zones:
        info["hr_zones"] = settings.hr_zones
    if settings.hr_zone_names:
        info["hr_zone_names"] = settings.hr_zone_names

    # Pace
    if settings.threshold_pace is not None:
        info["threshold_pace"] = settings.threshold_pace
    if settings.pace_units is not None:
        info["pace_units"] = settings.pace_units
    if settings.pace_zones:
        info["pace_zones"] = settings.pace_zones
    if settings.pace_zone_names:
        info["pace_zone_names"] = settings.pace_zone_names

    return info


async def get_sport_settings(
    ctx: Context | None = None,
) -> str:
    """Get all sport-specific settings (FTP, FTHR, pace thresholds, zones).

    Returns:
        Formatted list of sport settings with thresholds and zones
    """
    config = load_config()
    if not validate_credentials(config):
        return (
            "Error: Intervals.icu credentials not configured. Run intervals-icu-mcp-auth to set up."
        )

    try:
        async with ICUClient(config) as client:
            settings_list = await client.get_sport_settings()

            if not settings_list:
                return ResponseBuilder.build_response(
                    {"message": "No sport settings found"}, metadata={"count": 0}
                )

            settings_data = [_serialize_sport_settings(settings) for settings in settings_list]

            return ResponseBuilder.build_response(
                {"sport_settings": settings_data},
                analysis={
                    "zone_note": (
                        "power_zones/hr_zones/pace_zones are the upper boundary of each "
                        "zone as stored by intervals.icu (power/HR as a percent of "
                        "ftp/lthr; pace in native units). Combine with ftp, lthr, max_hr "
                        "and threshold_pace to derive absolute zone edges."
                    )
                },
                metadata={"count": len(settings_list), "type": "sport_settings_list"},
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")


async def update_sport_settings(
    sport_id: Annotated[int, "ID of the sport settings to update"],
    ftp: Annotated[int | None, "Functional Threshold Power in watts (for cycling)"] = None,
    fthr: Annotated[int | None, "Functional Threshold Heart Rate in bpm"] = None,
    pace_threshold: Annotated[
        float | None, "Threshold pace in min/km (e.g., 4.5 for 4:30/km)"
    ] = None,
    swim_threshold: Annotated[
        float | None, "Swim threshold in min/100m (e.g., 1.5 for 1:30/100m)"
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Update sport-specific settings (FTP, FTHR, pace thresholds).

    Args:
        sport_id: ID of the sport settings to update
        ftp: Functional Threshold Power in watts (optional)
        fthr: Functional Threshold Heart Rate in bpm (optional)
        pace_threshold: Threshold pace in min/km (optional)
        swim_threshold: Swim threshold in min/100m (optional)

    Returns:
        Updated sport settings
    """
    config = load_config()
    if not validate_credentials(config):
        return (
            "Error: Intervals.icu credentials not configured. Run intervals-icu-mcp-auth to set up."
        )

    try:
        async with ICUClient(config) as client:
            settings_data: dict[str, Any] = {}

            if ftp is not None:
                settings_data["ftp"] = ftp
            # Heart-rate threshold is `lthr` upstream, not `fthr` — the old key was
            # silently ignored by the API.
            if fthr is not None:
                settings_data["lthr"] = fthr
            # NOTE: pace/swim threshold writes are intentionally left on their legacy
            # keys. intervals.icu's `threshold_pace` is stored in native units we can't
            # reliably convert from the min/km|min/100m the params document, so routing
            # the value to `threshold_pace` without a verified conversion could corrupt
            # the athlete's pace zones. Reads are fixed; the pace *write* mapping is a
            # separate follow-on. See tests/test_sport_settings.py.
            if pace_threshold is not None:
                settings_data["pace_threshold"] = pace_threshold
            if swim_threshold is not None:
                settings_data["swim_threshold"] = swim_threshold

            if not settings_data:
                return ResponseBuilder.build_error_response(
                    "No fields provided to update", error_type="validation_error"
                )

            settings = await client.update_sport_settings(sport_id, settings_data)

            return ResponseBuilder.build_response(
                _serialize_sport_settings(settings),
                metadata={
                    "type": "sport_settings_updated",
                    "message": "Sport settings updated successfully",
                },
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")


async def apply_sport_settings(
    sport_id: Annotated[int, "ID of the sport settings to apply"],
    oldest_date: Annotated[
        str | None, "Oldest date to apply settings to (YYYY-MM-DD format)"
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Apply sport settings (zones, thresholds) to historical activities.

    This recalculates training load, zones, and other derived metrics for activities
    based on the current sport settings.

    Args:
        sport_id: ID of the sport settings to apply
        oldest_date: Oldest date to apply settings to (optional, defaults to all)

    Returns:
        Result of applying settings
    """
    config = load_config()
    if not validate_credentials(config):
        return (
            "Error: Intervals.icu credentials not configured. Run intervals-icu-mcp-auth to set up."
        )

    try:
        async with ICUClient(config) as client:
            result = await client.apply_sport_settings(sport_id, oldest=oldest_date)

            return ResponseBuilder.build_response(
                result,
                metadata={
                    "type": "sport_settings_applied",
                    "message": "Sport settings applied to activities successfully",
                },
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")


async def create_sport_settings(
    sport_type: Annotated[str, "Type of sport (e.g., 'Ride', 'Run', 'Swim')"],
    ftp: Annotated[int | None, "Functional Threshold Power in watts (for cycling)"] = None,
    fthr: Annotated[int | None, "Functional Threshold Heart Rate in bpm"] = None,
    pace_threshold: Annotated[
        float | None, "Threshold pace in min/km (e.g., 4.5 for 4:30/km)"
    ] = None,
    swim_threshold: Annotated[
        float | None, "Swim threshold in min/100m (e.g., 1.5 for 1:30/100m)"
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Create new sport-specific settings.

    Args:
        sport_type: Type of sport (e.g., 'Ride', 'Run', 'Swim')
        ftp: Functional Threshold Power in watts (optional)
        fthr: Functional Threshold Heart Rate in bpm (optional)
        pace_threshold: Threshold pace in min/km (optional)
        swim_threshold: Swim threshold in min/100m (optional)

    Returns:
        Created sport settings
    """
    config = load_config()
    if not validate_credentials(config):
        return (
            "Error: Intervals.icu credentials not configured. Run intervals-icu-mcp-auth to set up."
        )

    try:
        async with ICUClient(config) as client:
            # Upstream field is `types` (a list), not a scalar `type`.
            settings_data: dict[str, Any] = {"types": [sport_type]}

            if ftp is not None:
                settings_data["ftp"] = ftp
            # `lthr` upstream, not `fthr` (see update_sport_settings note).
            if fthr is not None:
                settings_data["lthr"] = fthr
            # Pace/swim writes left on legacy keys pending verified unit conversion.
            if pace_threshold is not None:
                settings_data["pace_threshold"] = pace_threshold
            if swim_threshold is not None:
                settings_data["swim_threshold"] = swim_threshold

            settings = await client.create_sport_settings(settings_data)

            return ResponseBuilder.build_response(
                _serialize_sport_settings(settings),
                metadata={
                    "type": "sport_settings_created",
                    "message": "Sport settings created successfully",
                },
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")


async def delete_sport_settings(
    sport_id: Annotated[int, "ID of the sport settings to delete"],
    ctx: Context | None = None,
) -> str:
    """Delete sport-specific settings.

    Args:
        sport_id: ID of the sport settings to delete

    Returns:
        Deletion confirmation
    """
    config = load_config()
    if not validate_credentials(config):
        return (
            "Error: Intervals.icu credentials not configured. Run intervals-icu-mcp-auth to set up."
        )

    try:
        async with ICUClient(config) as client:
            await client.delete_sport_settings(sport_id)

            return ResponseBuilder.build_response(
                {"sport_id": sport_id, "deleted": True},
                metadata={
                    "type": "sport_settings_deleted",
                    "message": "Sport settings deleted successfully",
                },
            )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), error_type="unexpected_error")
