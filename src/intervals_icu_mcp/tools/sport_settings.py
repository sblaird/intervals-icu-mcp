"""Sport-specific settings tools for FTP, FTHR, pace thresholds, and zones."""

from typing import Annotated, Any

from fastmcp import Context

from ..auth import load_config, validate_credentials
from ..client import ICUAPIError, ICUClient
from ..models import SportSettings
from ..response_builder import ResponseBuilder


def _min_per_km_to_mps(min_per_km: float) -> float:
    """Convert a running pace in decimal minutes/km to meters/second.

    intervals.icu stores ``threshold_pace`` in m/s (verified 2026-07-03). E.g.
    5.0 (5:00/km) -> 1000 / (5*60) = 3.333 m/s.
    """
    return 1000.0 / (min_per_km * 60.0)


def _min_per_100m_to_mps(min_per_100m: float) -> float:
    """Convert a swim pace in decimal minutes/100m to meters/second.

    E.g. 2.0 (2:00/100m) -> 100 / (2*60) = 0.8333 m/s (matches the real Swim entry).
    """
    return 100.0 / (min_per_100m * 60.0)


def _resolve_pace_mps(
    pace_threshold: float | None, swim_threshold: float | None
) -> tuple[float | None, str | None]:
    """Resolve the ``(threshold_pace m/s, display pace_units)`` from the human pace args.

    A sport is either run-paced or swim-paced, so supplying both is rejected. Raises
    ValueError on conflicting or non-positive input; returns ``(None, None)`` when no
    pace was given. ``pace_units`` is a sensible display default for a freshly created
    entry (updates leave the athlete's existing preference untouched).
    """
    if pace_threshold is not None and swim_threshold is not None:
        raise ValueError("Provide either pace_threshold or swim_threshold, not both.")
    if pace_threshold is not None:
        if pace_threshold <= 0:
            raise ValueError("pace_threshold must be a positive number of minutes per km.")
        return _min_per_km_to_mps(pace_threshold), "MINS_KM"
    if swim_threshold is not None:
        if swim_threshold <= 0:
            raise ValueError("swim_threshold must be a positive number of minutes per 100m.")
        return _min_per_100m_to_mps(swim_threshold), "SECS_100M"
    return None, None


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
                        "Zone arrays are the upper boundary of each zone, in the units "
                        "intervals.icu stores them (verified 2026-07-03): power_zones are "
                        "a % of FTP (a trailing 999 marks the open-ended top zone); "
                        "hr_zones are absolute bpm (top == max_hr); pace_zones and "
                        "threshold_pace are speeds in m/s. pace_units (e.g. MINS_KM) is "
                        "display-only."
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
        pace_threshold: Running threshold pace in min/km, e.g. 4.5 for 4:30/km (optional).
            Converted to m/s (intervals.icu's stored unit) before sending.
        swim_threshold: Swim threshold in min/100m, e.g. 1.5 for 1:30/100m (optional).
            Mutually exclusive with pace_threshold.

    Returns:
        Updated sport settings
    """
    config = load_config()
    if not validate_credentials(config):
        return (
            "Error: Intervals.icu credentials not configured. Run intervals-icu-mcp-auth to set up."
        )

    try:
        pace_mps, _pace_units = _resolve_pace_mps(pace_threshold, swim_threshold)
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    try:
        async with ICUClient(config) as client:
            settings_data: dict[str, Any] = {}

            if ftp is not None:
                settings_data["ftp"] = ftp
            # Heart-rate threshold is `lthr` upstream, not `fthr` — the old key was
            # silently ignored by the API.
            if fthr is not None:
                settings_data["lthr"] = fthr
            # threshold_pace is stored in m/s (verified 2026-07-03). Leave pace_units
            # alone on update so we don't clobber the athlete's display preference.
            if pace_mps is not None:
                settings_data["threshold_pace"] = pace_mps

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
        pace_threshold: Running threshold pace in min/km, e.g. 4.5 for 4:30/km (optional).
        swim_threshold: Swim threshold in min/100m, e.g. 1.5 for 1:30/100m (optional).
            Mutually exclusive with pace_threshold.

    Returns:
        Created sport settings
    """
    config = load_config()
    if not validate_credentials(config):
        return (
            "Error: Intervals.icu credentials not configured. Run intervals-icu-mcp-auth to set up."
        )

    try:
        pace_mps, pace_units = _resolve_pace_mps(pace_threshold, swim_threshold)
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    try:
        async with ICUClient(config) as client:
            # Upstream field is `types` (a list), not a scalar `type`.
            post_body: dict[str, Any] = {"types": [sport_type]}

            if ftp is not None:
                post_body["ftp"] = ftp
            # `lthr` upstream, not `fthr` (see update_sport_settings).
            if fthr is not None:
                post_body["lthr"] = fthr
            # Set a sensible display unit for a new entry (updates leave it alone).
            if pace_units is not None:
                post_body["pace_units"] = pace_units

            settings = await client.create_sport_settings(post_body)

            # intervals.icu silently drops threshold_pace on create — it only sticks via
            # a follow-up PUT (verified 2026-07-03). Stored in m/s.
            if pace_mps is not None and settings.id is not None:
                settings = await client.update_sport_settings(
                    settings.id, {"threshold_pace": pace_mps}
                )

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
