"""Async HTTP client for Intervals.icu API."""

import asyncio
import logging
import random
from typing import Any, Generic, NamedTuple, TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

from .auth import ICUConfig
from .models import (
    Activity,
    ActivitySearchResult,
    ActivityStreams,
    ActivitySummary,
    Athlete,
    BestEffort,
    Event,
    Folder,
    Gear,
    GearReminder,
    Histogram,
    HRCurve,
    Interval,
    PaceCurve,
    PowerCurve,
    SportSettings,
    Wellness,
    Workout,
)

logger = logging.getLogger(__name__)


class StreamsResult(NamedTuple):
    """An ActivityStreams plus the names of any streams dropped while parsing."""

    streams: ActivityStreams
    dropped: list[str]


def _build_streams_resilient(raw: dict[str, Any]) -> StreamsResult:
    """Build an ActivityStreams, dropping only the individual streams that fail.

    A single malformed stream (historically a flat/garbage ``latlng`` from the
    upstream API) used to raise a ValidationError that discarded the entire
    streams response. Instead, validate all streams together and, on failure,
    drop just the offending top-level fields and retry, so the well-formed
    streams (watts, heartrate, etc.) still come through.

    Returns the parsed streams alongside the names of any dropped streams so
    callers can tell the LLM the response is partial.
    """
    data = dict(raw)
    dropped: list[str] = []
    # Bounded retries: each pass removes at least one bad field, so at most
    # len(data) passes are needed before we either succeed or run out.
    for _ in range(len(data) + 1):
        try:
            return StreamsResult(ActivityStreams(**data), dropped)
        except ValidationError as exc:
            bad_fields = {
                str(err["loc"][0]) for err in exc.errors() if err.get("loc")
            } & data.keys()
            if not bad_fields:
                raise
            for field in bad_fields:
                logger.warning("Dropping malformed activity stream %r", field)
                data.pop(field, None)
                dropped.append(field)
    return StreamsResult(ActivityStreams(), dropped)


class ICUAPIError(Exception):
    """Custom exception for Intervals.icu API errors."""

    def __init__(self, message: str, status_code: int | None = None):
        """Initialize API error.

        Args:
            message: Error message
            status_code: HTTP status code if available
        """
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


ModelT = TypeVar("ModelT", bound=BaseModel)


class ParsedList(NamedTuple, Generic[ModelT]):
    """A resiliently-parsed list plus info about any items dropped (R6)."""

    items: list[ModelT]
    dropped: list[dict[str, Any]]


def parse_list_resilient(raw: Any, model: type[ModelT], *, label: str) -> ParsedList[ModelT]:
    """Validate a list payload per-item, dropping only the items that fail.

    Atomic ``TypeAdapter(list[Model]).validate_python`` fails the entire call
    when one item drifts — the exact mechanism behind the latlng and
    SportSettings breakages. Instead, validate each item and drop only the
    malformed ones, logging what was dropped so failures stay diagnosable
    from Cloud Run logs.

    Args:
        raw: The decoded JSON payload (must be a list).
        model: Pydantic model to validate each item against.
        label: Human-readable item label for logs/metadata.

    Returns:
        ParsedList of validated items plus per-drop info
        (``{"index": int, "fields": [str, ...]}``).

    Raises:
        ICUAPIError: If the payload is not a list at all (an error body, not
            item drift).
    """
    if not isinstance(raw, list):
        raise ICUAPIError(f"Expected a list of {label} items, got {type(raw).__name__}")
    items: list[ModelT] = []
    dropped: list[dict[str, Any]] = []
    for index, entry in enumerate(cast("list[Any]", raw)):
        try:
            items.append(model.model_validate(entry))
        except ValidationError as exc:
            fields = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
            logger.warning(
                "Dropping malformed %s item %d (bad fields: %s)",
                label,
                index,
                ", ".join(fields) or "<unknown>",
            )
            dropped.append({"index": index, "fields": fields})
    return ParsedList(items, dropped)


def dropped_items_metadata(dropped: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    """Metadata fragment for tool responses when a parsed list is partial.

    Empty dict when nothing was dropped; otherwise dropped_count (+ which
    fields failed, for small N) so the LLM doesn't read a partial list as
    complete.
    """
    if not dropped:
        return {}
    meta: dict[str, Any] = {
        "partial": True,
        "dropped_count": len(dropped),
        "message": f"{len(dropped)} {label} item(s) were malformed upstream and omitted",
    }
    if len(dropped) <= 5:
        meta["dropped_items"] = dropped
    return meta


class ICUClient:
    """Async HTTP client for Intervals.icu API with automatic error handling."""

    BASE_URL = "https://intervals.icu/api/v1"

    def __init__(self, config: ICUConfig):
        """Initialize the Intervals.icu API client.

        Args:
            config: ICUConfig with API credentials
        """
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ICUClient":
        """Async context manager entry."""
        # Use Basic Auth with username "API_KEY" and password as the actual API key
        auth = httpx.BasicAuth(username="API_KEY", password=self.config.intervals_icu_api_key)

        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=30.0,
            auth=auth,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()

    # R8 (STB-M3): retry budget for transient upstream failures. Worst case
    # (2 retries at 0.5s/1s + jitter) stays well inside the 30s client timeout.
    MAX_RETRIES = 2
    RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
    RETRY_BASE_DELAY_SECONDS = 0.5
    RETRY_MAX_DELAY_SECONDS = 10.0

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        """Jittered exponential backoff; a parseable Retry-After header wins."""
        if retry_after:
            try:
                return min(float(retry_after), self.RETRY_MAX_DELAY_SECONDS)
            except ValueError:
                pass
        return self.RETRY_BASE_DELAY_SECONDS * (2**attempt) + random.uniform(0, 0.25)

    def _finalize_response(
        self, response: httpx.Response, method: str, endpoint: str
    ) -> httpx.Response:
        """Map error statuses to ICUAPIError; return successful responses."""
        if response.status_code == 401:
            raise ICUAPIError("Unauthorized. Check your API key and athlete ID.", 401)

        if response.status_code == 404:
            raise ICUAPIError("Resource not found.", 404)

        if response.status_code == 429:
            raise ICUAPIError("Rate limit exceeded. Please try again later.", 429)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response.text else ""
            logger.warning(
                "ICU API HTTP %s on %s %s | body=%s",
                e.response.status_code,
                method,
                endpoint,
                body,
            )
            raise ICUAPIError(
                f"HTTP {e.response.status_code}: {body}",
                e.response.status_code,
            ) from e
        return response

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated request to the API.

        Transient failures (429/500/502/503/504 and connection errors) are
        retried up to MAX_RETRIES times with jittered exponential backoff,
        honouring Retry-After on 429 (R8). Other 4xx fail immediately.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint path
            **kwargs: Additional arguments for httpx.request

        Returns:
            httpx.Response object

        Raises:
            ICUAPIError: If the request fails
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async context manager.")

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = await self._client.request(method, endpoint, **kwargs)
            except httpx.RequestError as e:
                if attempt < self.MAX_RETRIES:
                    delay = self._retry_delay(attempt, None)
                    logger.warning(
                        "ICU API request error on %s %s (attempt %d/%d): %s; retrying in %.2fs",
                        method,
                        endpoint,
                        attempt + 1,
                        self.MAX_RETRIES + 1,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning("ICU API request failed on %s %s: %s", method, endpoint, e)
                raise ICUAPIError(f"Request failed: {str(e)}") from e

            if response.status_code in self.RETRYABLE_STATUS_CODES and attempt < self.MAX_RETRIES:
                delay = self._retry_delay(attempt, response.headers.get("Retry-After"))
                logger.warning(
                    "ICU API HTTP %d on %s %s (attempt %d/%d); retrying in %.2fs",
                    response.status_code,
                    method,
                    endpoint,
                    attempt + 1,
                    self.MAX_RETRIES + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            return self._finalize_response(response, method, endpoint)

        raise AssertionError("unreachable: retry loop always returns or raises")

    # ==================== Athlete Endpoints ====================

    async def get_athlete(self, athlete_id: str | None = None) -> Athlete:
        """Get athlete profile with sport settings.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Athlete model with full profile information
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}")
        return Athlete(**response.json())

    # ==================== Activity Endpoints ====================

    async def get_activities(
        self,
        athlete_id: str | None = None,
        oldest: str | None = None,
        newest: str | None = None,
        limit: int = 30,
    ) -> ParsedList[ActivitySummary]:
        """List activities for a date range.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            oldest: Oldest date to fetch (ISO-8601 format)
            newest: Newest date to fetch (ISO-8601 format)
            limit: Maximum number of activities to return

        Returns:
            ParsedList of ActivitySummary objects plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params = {}

        if oldest:
            params["oldest"] = oldest
        if newest:
            params["newest"] = newest

        response = await self._request("GET", f"/athlete/{athlete_id}/activities", params=params)
        parsed = parse_list_resilient(response.json(), ActivitySummary, label="activity")
        return ParsedList(parsed.items[:limit], parsed.dropped)

    async def get_activity(self, athlete_id: str | None = None, activity_id: str = "") -> Activity:
        """Get detailed activity information.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            activity_id: Activity ID to fetch

        Returns:
            Activity model with full details
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/activity/{activity_id}")
        return Activity(**response.json())

    async def search_activities(
        self,
        athlete_id: str | None = None,
        query: str = "",
        limit: int = 30,
    ) -> ParsedList[ActivitySearchResult]:
        """Search for activities by name or tag.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            query: Search query (name or tag)
            limit: Maximum number of results to return

        Returns:
            ParsedList of ActivitySearchResult objects plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params = {"q": query}

        response = await self._request(
            "GET", f"/athlete/{athlete_id}/activities/search", params=params
        )
        parsed = parse_list_resilient(
            response.json(), ActivitySearchResult, label="activity search result"
        )
        return ParsedList(parsed.items[:limit], parsed.dropped)

    async def search_activities_full(
        self,
        athlete_id: str | None = None,
        query: str = "",
        limit: int = 30,
    ) -> ParsedList[Activity]:
        """Search for activities by name or tag, returning full Activity objects.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            query: Search query (name or tag)
            limit: Maximum number of results to return

        Returns:
            ParsedList of full Activity objects plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params = {"q": query}

        response = await self._request(
            "GET", f"/athlete/{athlete_id}/activities/search-full", params=params
        )
        parsed = parse_list_resilient(response.json(), Activity, label="activity")
        return ParsedList(parsed.items[:limit], parsed.dropped)

    async def get_activities_around(
        self,
        activity_id: str,
        athlete_id: str | None = None,
        count: int = 5,
    ) -> ParsedList[Activity]:
        """Get activities before and after a specific activity.

        Args:
            activity_id: The reference activity ID
            athlete_id: Athlete ID (uses config default if not provided)
            count: Number of activities to return before and after (default 5)

        Returns:
            ParsedList of Activity objects plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params = {"id": activity_id, "count": count}

        response = await self._request(
            "GET", f"/athlete/{athlete_id}/activities-around", params=params
        )
        return parse_list_resilient(response.json(), Activity, label="activity")

    async def update_activity(
        self,
        activity_id: str,
        activity_data: dict[str, Any],
    ) -> Activity:
        """Update an existing activity.

        Args:
            activity_id: Activity ID
            activity_data: Activity data dictionary with fields to update

        Returns:
            Updated Activity object
        """
        response = await self._request("PUT", f"/activity/{activity_id}", json=activity_data)
        return Activity(**response.json())

    async def delete_activity(
        self,
        activity_id: str,
    ) -> bool:
        """Delete an activity.

        Args:
            activity_id: Activity ID

        Returns:
            True if deletion was successful
        """
        await self._request("DELETE", f"/activity/{activity_id}")
        return True

    async def download_activity_file(
        self,
        activity_id: str,
    ) -> bytes:
        """Download the original activity file.

        Args:
            activity_id: Activity ID

        Returns:
            File content as bytes
        """
        response = await self._request("GET", f"/activity/{activity_id}/file")
        return response.content

    async def download_fit_file(
        self,
        activity_id: str,
    ) -> bytes:
        """Download activity as FIT file.

        Args:
            activity_id: Activity ID

        Returns:
            FIT file content as bytes
        """
        response = await self._request("GET", f"/activity/{activity_id}/fit-file")
        return response.content

    async def download_gpx_file(
        self,
        activity_id: str,
    ) -> bytes:
        """Download activity as GPX file.

        Args:
            activity_id: Activity ID

        Returns:
            GPX file content as bytes
        """
        response = await self._request("GET", f"/activity/{activity_id}/gpx-file")
        return response.content

    async def get_power_histogram(
        self,
        activity_id: str,
    ) -> Histogram:
        """Get power distribution histogram for an activity.

        Args:
            activity_id: Activity ID

        Returns:
            Histogram with power distribution bins
        """
        response = await self._request("GET", f"/activity/{activity_id}/power-histogram")
        return Histogram(**response.json())

    async def get_hr_histogram(
        self,
        activity_id: str,
    ) -> Histogram:
        """Get heart rate distribution histogram for an activity.

        Args:
            activity_id: Activity ID

        Returns:
            Histogram with HR distribution bins
        """
        response = await self._request("GET", f"/activity/{activity_id}/hr-histogram")
        return Histogram(**response.json())

    async def get_pace_histogram(
        self,
        activity_id: str,
    ) -> Histogram:
        """Get pace distribution histogram for an activity.

        Args:
            activity_id: Activity ID

        Returns:
            Histogram with pace distribution bins
        """
        response = await self._request("GET", f"/activity/{activity_id}/pace-histogram")
        return Histogram(**response.json())

    async def get_gap_histogram(
        self,
        activity_id: str,
    ) -> Histogram:
        """Get grade-adjusted pace (GAP) histogram for an activity.

        Args:
            activity_id: Activity ID

        Returns:
            Histogram with GAP distribution bins
        """
        response = await self._request("GET", f"/activity/{activity_id}/gap-histogram")
        return Histogram(**response.json())

    # ==================== Wellness Endpoints ====================

    async def get_wellness(
        self,
        athlete_id: str | None = None,
        oldest: str | None = None,
        newest: str | None = None,
    ) -> ParsedList[Wellness]:
        """Get wellness records for a date range.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            oldest: Oldest date to fetch (ISO-8601 format)
            newest: Newest date to fetch (ISO-8601 format)

        Returns:
            ParsedList of Wellness records plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params = {}

        if oldest:
            params["oldest"] = oldest
        if newest:
            params["newest"] = newest

        response = await self._request("GET", f"/athlete/{athlete_id}/wellness", params=params)
        return parse_list_resilient(response.json(), Wellness, label="wellness")

    async def get_wellness_for_date(
        self,
        date: str,
        athlete_id: str | None = None,
    ) -> Wellness:
        """Get wellness record for a specific date.

        Args:
            date: Date in ISO-8601 format (YYYY-MM-DD)
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Wellness record for the specified date
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}/wellness/{date}")
        return Wellness(**response.json())

    async def update_wellness(
        self,
        wellness_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> Wellness:
        """Update wellness record (creates if doesn't exist).

        Args:
            wellness_data: Wellness data dictionary (must include 'id' as date)
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Updated Wellness record
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("PUT", f"/athlete/{athlete_id}/wellness", json=wellness_data)
        return Wellness(**response.json())

    async def update_wellness_by_date(
        self,
        date: str,
        wellness_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> Wellness:
        """Update wellness record for a specific date.

        Args:
            date: Date in ISO-8601 format (YYYY-MM-DD)
            wellness_data: Wellness data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Updated Wellness record
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "PUT", f"/athlete/{athlete_id}/wellness/{date}", json=wellness_data
        )
        return Wellness(**response.json())

    async def update_wellness_bulk(
        self,
        wellness_records: list[dict[str, Any]],
        athlete_id: str | None = None,
    ) -> ParsedList[Wellness]:
        """Bulk update wellness records.

        Args:
            wellness_records: List of wellness data dictionaries (each must include 'id' as date)
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            ParsedList of updated Wellness records plus any dropped items
            (the write succeeded; drops mean the echo was unparseable)
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "PUT", f"/athlete/{athlete_id}/wellness-bulk", json=wellness_records
        )
        return parse_list_resilient(response.json(), Wellness, label="wellness")

    # ==================== Event/Calendar Endpoints ====================

    async def get_events(
        self,
        athlete_id: str | None = None,
        oldest: str | None = None,
        newest: str | None = None,
    ) -> ParsedList[Event]:
        """Get calendar events (planned workouts, notes, races).

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            oldest: Oldest date to fetch (ISO-8601 format)
            newest: Newest date to fetch (ISO-8601 format)

        Returns:
            ParsedList of Event objects plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params = {}

        if oldest:
            params["oldest"] = oldest
        if newest:
            params["newest"] = newest

        response = await self._request("GET", f"/athlete/{athlete_id}/events", params=params)
        return parse_list_resilient(response.json(), Event, label="event")

    async def get_event(
        self,
        event_id: int,
        athlete_id: str | None = None,
    ) -> Event:
        """Get a specific event.

        Args:
            event_id: Event ID
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Event object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}/events/{event_id}")
        return Event(**response.json())

    # ==================== Performance Curve Endpoints ====================

    async def get_power_curves(
        self,
        athlete_id: str | None = None,
        oldest: str | None = None,
        newest: str | None = None,
        activity_type: str = "Ride",
    ) -> PowerCurve:
        """Get power curve data (best efforts for various durations).

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            oldest: Oldest date to include (ISO-8601 format)
            newest: Newest date to include (ISO-8601 format)
            activity_type: ActivityType filter (e.g., "Ride", "VirtualRide"). Required by API.

        Returns:
            PowerCurve with best efforts data
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params: dict[str, str] = {"type": activity_type}

        if oldest:
            params["oldest"] = oldest
        if newest:
            params["newest"] = newest

        response = await self._request("GET", f"/athlete/{athlete_id}/power-curves", params=params)
        return PowerCurve(**response.json())

    async def get_hr_curves(
        self,
        athlete_id: str | None = None,
        oldest: str | None = None,
        newest: str | None = None,
        activity_type: str = "Ride",
    ) -> HRCurve:
        """Get heart rate curve data (best efforts for various durations).

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            oldest: Oldest date to include (ISO-8601 format)
            newest: Newest date to include (ISO-8601 format)
            activity_type: ActivityType filter (e.g., "Ride", "Run"). Required by API.

        Returns:
            HRCurve with best efforts data
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params: dict[str, str] = {"type": activity_type}

        if oldest:
            params["oldest"] = oldest
        if newest:
            params["newest"] = newest

        response = await self._request("GET", f"/athlete/{athlete_id}/hr-curves", params=params)
        return HRCurve(**response.json())

    async def get_pace_curves(
        self,
        athlete_id: str | None = None,
        oldest: str | None = None,
        newest: str | None = None,
        use_gap: bool = False,
        activity_type: str = "Run",
    ) -> PaceCurve:
        """Get pace curve data (best efforts for various durations).

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            oldest: Oldest date to include (ISO-8601 format)
            newest: Newest date to include (ISO-8601 format)
            use_gap: Use Grade Adjusted Pace for running (default False)
            activity_type: ActivityType filter (e.g., "Run", "VirtualRun"). Required by API.

        Returns:
            PaceCurve with best efforts data
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params: dict[str, str] = {"type": activity_type}

        if oldest:
            params["oldest"] = oldest
        if newest:
            params["newest"] = newest
        if use_gap:
            params["gap"] = "true"

        response = await self._request("GET", f"/athlete/{athlete_id}/pace-curves", params=params)
        return PaceCurve(**response.json())

    # ==================== Workout Library Endpoints ====================

    async def get_workout_folders(
        self,
        athlete_id: str | None = None,
    ) -> ParsedList[Folder]:
        """Get workout folders and training plans.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            ParsedList of folders/plans plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}/folders")
        return parse_list_resilient(response.json(), Folder, label="folder")

    # ==================== Activity Analysis Endpoints ====================

    async def get_activity_intervals(
        self,
        activity_id: str,
    ) -> ParsedList[Interval]:
        """Get intervals for a specific activity.

        Args:
            activity_id: Activity ID

        Returns:
            ParsedList of Interval objects plus any dropped items
        """
        response = await self._request("GET", f"/activity/{activity_id}/intervals")
        payload = response.json()
        # The endpoint returns an activity wrapper {"id":..., "icu_intervals":[...], ...}
        # rather than a bare list of intervals.
        intervals_list: Any
        if isinstance(payload, dict):
            payload_dict = cast("dict[str, Any]", payload)
            intervals_list = (
                payload_dict.get("icu_intervals") or payload_dict.get("intervals") or []
            )
        else:
            intervals_list = payload
        return parse_list_resilient(intervals_list, Interval, label="interval")

    async def get_activity_streams(
        self,
        activity_id: str,
        streams: list[str] | None = None,
    ) -> StreamsResult:
        """Get time-series data streams for an activity.

        Args:
            activity_id: Activity ID
            streams: List of stream types to fetch (e.g., ["watts", "heartrate"])
                    If None, fetches all available streams

        Returns:
            StreamsResult: the parsed ActivityStreams plus any dropped stream names
        """
        params: dict[str, str] = {}
        if streams:
            params["types"] = ",".join(streams)

        response = await self._request("GET", f"/activity/{activity_id}/streams", params=params)
        payload = response.json()
        # API returns a list of {"type": "<stream-name>", "data": [...]} entries.
        # ActivityStreams expects per-stream-name fields, so reshape.
        if isinstance(payload, list):
            streams_dict: dict[str, Any] = {}
            for entry in cast("list[Any]", payload):
                if not isinstance(entry, dict):
                    continue
                entry_dict = cast("dict[str, Any]", entry)
                name = entry_dict.get("type")
                data = entry_dict.get("data")
                if name and data is not None:
                    streams_dict[name] = data
            return _build_streams_resilient(streams_dict)
        if isinstance(payload, dict):
            return _build_streams_resilient(cast(dict[str, Any], payload))
        return _build_streams_resilient({})

    async def get_best_efforts(
        self,
        activity_id: str,
        stream: str = "watts",
    ) -> ParsedList[BestEffort]:
        """Get best efforts for an activity.

        Args:
            activity_id: Activity ID
            stream: Stream to compute best efforts for (e.g., "watts", "heartrate", "pace").
                Required by the API; defaults to "watts" for cycling.

        Returns:
            ParsedList of BestEffort objects plus any dropped items
        """
        response = await self._request(
            "GET",
            f"/activity/{activity_id}/best-efforts",
            params={"stream": stream},
        )
        return parse_list_resilient(response.json(), BestEffort, label="best effort")

    async def get_power_vs_hr(self, activity_id: str) -> dict[str, Any]:
        """Get power-vs-HR plot data for an activity (aerobic decoupling).

        Args:
            activity_id: Activity ID

        Returns:
            Raw plot data as a dict (not modeled — pass through to caller).
        """
        response = await self._request("GET", f"/activity/{activity_id}/power-vs-hr")
        return response.json()

    async def get_time_at_hr(self, activity_id: str) -> dict[str, Any]:
        """Get time-at-HR distribution for an activity.

        Args:
            activity_id: Activity ID

        Returns:
            Raw plot data as a dict.
        """
        response = await self._request("GET", f"/activity/{activity_id}/time-at-hr")
        return response.json()

    async def search_intervals(
        self,
        athlete_id: str | None = None,
        interval_type: str | None = None,
        min_duration: int | None = None,
        max_duration: int | None = None,
        min_intensity: int | None = None,
        max_intensity: int | None = None,
        min_reps: int | None = None,
        max_reps: int | None = None,
        limit: int = 30,
        activity_type: str = "Ride",
    ) -> list[dict[str, Any]]:
        """Search for intervals across activities.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)
            interval_type: Type of interval to search for
            min_duration: Minimum duration in seconds (sent as `minSecs`)
            max_duration: Maximum duration in seconds (sent as `maxSecs`)
            min_intensity: Minimum intensity as % of threshold (sent as `minIntensity`)
            max_intensity: Maximum intensity as % of threshold (sent as `maxIntensity`)
            min_reps: Minimum repetitions in the interval block
            max_reps: Maximum repetitions in the interval block
            limit: Maximum number of results to return
            activity_type: ActivityType filter required by the API (e.g., "Ride", "Run")

        Returns:
            List of matching intervals with activity context
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id

        # The API requires minSecs/maxSecs/minIntensity/maxIntensity on every call (422
        # otherwise). Fill in wide-open defaults when the caller doesn't constrain them.
        params: dict[str, Any] = {
            "type": activity_type,
            "limit": limit,
            "minSecs": min_duration if min_duration is not None else 0,
            "maxSecs": max_duration if max_duration is not None else 86400,
            "minIntensity": min_intensity if min_intensity is not None else 0,
            "maxIntensity": max_intensity if max_intensity is not None else 1000,
        }

        if interval_type:
            params["intervalType"] = interval_type
        if min_reps is not None:
            params["minReps"] = min_reps
        if max_reps is not None:
            params["maxReps"] = max_reps

        response = await self._request(
            "GET", f"/athlete/{athlete_id}/activities/interval-search", params=params
        )
        results = response.json()
        return results[:limit]

    # ==================== Workout Library Endpoints ====================

    async def get_workouts_in_folder(
        self,
        folder_id: int,
        athlete_id: str | None = None,
    ) -> ParsedList[Workout]:
        """Get workouts in a specific folder.

        Args:
            folder_id: Folder ID
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            ParsedList of Workout objects plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}/folders/{folder_id}/workouts")
        return parse_list_resilient(response.json(), Workout, label="workout")

    # ==================== Event Write Operations ====================

    async def create_event(
        self,
        event_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> Event:
        """Create a new calendar event.

        Args:
            event_data: Event data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Created Event object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("POST", f"/athlete/{athlete_id}/events", json=event_data)
        return Event(**response.json())

    async def update_event(
        self,
        event_id: int,
        event_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> Event:
        """Update an existing calendar event.

        Args:
            event_id: Event ID
            event_data: Updated event data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Updated Event object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "PUT", f"/athlete/{athlete_id}/events/{event_id}", json=event_data
        )
        return Event(**response.json())

    async def delete_event(
        self,
        event_id: int,
        athlete_id: str | None = None,
    ) -> bool:
        """Delete a calendar event.

        Args:
            event_id: Event ID
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            True if deletion was successful
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        await self._request("DELETE", f"/athlete/{athlete_id}/events/{event_id}")
        return True

    # ==================== Gear Endpoints ====================

    async def get_gear(
        self,
        athlete_id: str | None = None,
    ) -> ParsedList[Gear]:
        """Get all gear items for an athlete.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            ParsedList of Gear objects plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}/gear")
        return parse_list_resilient(response.json(), Gear, label="gear")

    async def create_gear(
        self,
        gear_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> Gear:
        """Create a new gear item.

        Args:
            gear_data: Gear data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Created Gear object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("POST", f"/athlete/{athlete_id}/gear", json=gear_data)
        return Gear(**response.json())

    async def update_gear(
        self,
        gear_id: str,
        gear_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> Gear:
        """Update an existing gear item.

        Args:
            gear_id: Gear ID
            gear_data: Updated gear data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Updated Gear object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "PUT", f"/athlete/{athlete_id}/gear/{gear_id}", json=gear_data
        )
        return Gear(**response.json())

    async def delete_gear(
        self,
        gear_id: str,
        athlete_id: str | None = None,
    ) -> bool:
        """Delete a gear item.

        Args:
            gear_id: Gear ID
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            True if deletion was successful
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        await self._request("DELETE", f"/athlete/{athlete_id}/gear/{gear_id}")
        return True

    async def create_gear_reminder(
        self,
        gear_id: str,
        reminder_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> GearReminder:
        """Create a new reminder for a gear item.

        Args:
            gear_id: Gear ID
            reminder_data: Reminder data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Created GearReminder object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "POST", f"/athlete/{athlete_id}/gear/{gear_id}/reminders", json=reminder_data
        )
        return GearReminder(**response.json())

    async def update_gear_reminder(
        self,
        gear_id: str,
        reminder_id: int,
        reminder_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> GearReminder:
        """Update an existing gear reminder.

        Args:
            gear_id: Gear ID
            reminder_id: Reminder ID
            reminder_data: Updated reminder data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Updated GearReminder object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "PUT",
            f"/athlete/{athlete_id}/gear/{gear_id}/reminders/{reminder_id}",
            json=reminder_data,
        )
        return GearReminder(**response.json())

    # ==================== Sport Settings Endpoints ====================

    async def get_sport_settings(
        self,
        athlete_id: str | None = None,
    ) -> ParsedList[SportSettings]:
        """Get sport settings for an athlete.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            ParsedList of SportSettings objects plus any dropped items
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}/sport-settings")
        return parse_list_resilient(response.json(), SportSettings, label="sport settings")

    async def update_sport_settings(
        self,
        sport_id: int,
        settings_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> SportSettings:
        """Update sport-specific settings (FTP, FTHR, pace threshold, etc.).

        Args:
            sport_id: Sport settings ID
            settings_data: Updated settings data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Updated SportSettings object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "PUT", f"/athlete/{athlete_id}/sport-settings/{sport_id}", json=settings_data
        )
        return SportSettings(**response.json())

    async def apply_sport_settings(
        self,
        sport_id: int,
        oldest: str | None = None,
        athlete_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply sport settings (zones, thresholds) to historical activities.

        Args:
            sport_id: Sport settings ID
            oldest: Oldest date to apply settings to (ISO-8601 format)
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Result of applying settings
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params = {}
        if oldest:
            params["oldest"] = oldest

        response = await self._request(
            "POST", f"/athlete/{athlete_id}/sport-settings/{sport_id}/apply", params=params
        )
        return response.json()

    async def create_sport_settings(
        self,
        settings_data: dict[str, Any],
        athlete_id: str | None = None,
    ) -> SportSettings:
        """Create new sport settings.

        Args:
            settings_data: Sport settings data dictionary
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Created SportSettings object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "POST", f"/athlete/{athlete_id}/sport-settings", json=settings_data
        )
        return SportSettings(**response.json())

    async def delete_sport_settings(
        self,
        sport_id: int,
        athlete_id: str | None = None,
    ) -> bool:
        """Delete sport settings.

        Args:
            sport_id: Sport settings ID
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            True if deletion was successful
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        await self._request("DELETE", f"/athlete/{athlete_id}/sport-settings/{sport_id}")
        return True

    # ==================== Bulk Event Operations ====================

    async def bulk_create_events(
        self,
        events_data: list[dict[str, Any]],
        athlete_id: str | None = None,
    ) -> ParsedList[Event]:
        """Create multiple calendar events in a single request.

        Args:
            events_data: List of event data dictionaries
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            ParsedList of created Event objects plus any dropped items
            (the write succeeded; drops mean the echo was unparseable)
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "POST", f"/athlete/{athlete_id}/events/bulk", json=events_data
        )
        return parse_list_resilient(response.json(), Event, label="event")

    async def bulk_delete_events(
        self,
        event_ids: list[int],
        athlete_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete multiple calendar events in a single request.

        Why this is PUT /events/bulk-delete and not DELETE /events/bulk:
        the upstream router matches /events/{id} before /events/bulk, so
        DELETE /events/bulk hits the single-event handler with id="bulk"
        and returns NumberFormatException. The actual bulk-delete endpoint
        is PUT /events/bulk-delete with a body of [{id}, ...] DoomedEvent
        records.

        Args:
            event_ids: List of event IDs to delete
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Result of bulk deletion (raw upstream response)
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        body = [{"id": eid} for eid in event_ids]
        response = await self._request(
            "PUT", f"/athlete/{athlete_id}/events/bulk-delete", json=body
        )
        # Some bulk-delete responses are empty; tolerate that.
        try:
            return response.json()
        except ValueError:
            return {"deleted": event_ids}

    async def duplicate_event(
        self,
        event_id: int,
        new_date: str,
        athlete_id: str | None = None,
    ) -> Event:
        """Duplicate an existing event to a new date.

        Args:
            event_id: Event ID to duplicate
            new_date: New date for the duplicated event (ISO-8601 format)
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Created Event object
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "POST",
            f"/athlete/{athlete_id}/events/{event_id}/duplicate",
            json={"start_date_local": new_date},
        )
        return Event(**response.json())

    async def mark_event_done(
        self,
        event_id: int,
        athlete_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark a planned event as done by creating a manual matching activity.

        Args:
            event_id: Planned event ID to mark done
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Raw activity dict the server created.
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("POST", f"/athlete/{athlete_id}/events/{event_id}/mark-done")
        return response.json()

    # ==================== Weather ====================

    async def get_weather_forecast(self, athlete_id: str | None = None) -> dict[str, Any]:
        """Get weather forecast for the athlete's planned events.

        Args:
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            Raw forecast dict (WeatherDTO).
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}/weather-forecast")
        return response.json()

    async def get_activity_weather(
        self,
        activity_id: str,
        start_index: int | None = None,
        end_index: int | None = None,
    ) -> dict[str, Any]:
        """Get weather summary recorded for an activity.

        Args:
            activity_id: Activity ID
            start_index: Optional stream-index start for partial windows
            end_index: Optional stream-index end for partial windows

        Returns:
            Raw weather summary dict.
        """
        params: dict[str, Any] = {}
        if start_index is not None:
            params["start_index"] = start_index
        if end_index is not None:
            params["end_index"] = end_index
        response = await self._request(
            "GET",
            f"/activity/{activity_id}/weather-summary",
            params=params if params else None,
        )
        return response.json()

    # ==================== Routes ====================

    async def list_routes(self, athlete_id: str | None = None) -> list[dict[str, Any]]:
        """List all routes for an athlete (path data NOT included).

        Args:
            athlete_id: Athlete ID (uses config default if not provided)

        Returns:
            List of route summaries with activity counts.
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request("GET", f"/athlete/{athlete_id}/routes")
        data = response.json()
        return cast("list[dict[str, Any]]", data) if isinstance(data, list) else []

    async def get_route(
        self,
        route_id: int,
        athlete_id: str | None = None,
        include_path: bool = False,
    ) -> dict[str, Any]:
        """Get a single route. By default the path/latlngs are NOT included.

        Args:
            route_id: Route ID
            athlete_id: Athlete ID (uses config default if not provided)
            include_path: If True, response includes the latlng path (large payload)

        Returns:
            Raw route dict.
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        params: dict[str, Any] | None = {"includePath": "true"} if include_path else None
        response = await self._request(
            "GET", f"/athlete/{athlete_id}/routes/{route_id}", params=params
        )
        return response.json()

    async def get_route_similarity(
        self,
        route_id: int,
        other_route_id: int,
        athlete_id: str | None = None,
        include_paths: bool = False,
    ) -> dict[str, Any]:
        """Compute similarity between two of the athlete's routes.

        Args:
            route_id: First route ID
            other_route_id: Second route ID to compare against
            athlete_id: Athlete ID (uses config default if not provided)
            include_paths: If True, keep both routes' latlng path arrays
                (large payload). Default strips them — the upstream endpoint
                has no exclude option, so this is done client-side — leaving
                the similarity metrics and bounds.

        Returns:
            Raw RouteSimilarity dict.
        """
        athlete_id = athlete_id or self.config.intervals_icu_athlete_id
        response = await self._request(
            "GET",
            f"/athlete/{athlete_id}/routes/{route_id}/similarity/{other_route_id}",
        )
        payload: Any = response.json()
        if not include_paths and isinstance(payload, dict):
            for key in ("route", "other"):
                embedded = cast("dict[str, Any]", payload).get(key)
                if isinstance(embedded, dict):
                    cast("dict[str, Any]", embedded).pop("latlngs", None)
        return cast("dict[str, Any]", payload)
