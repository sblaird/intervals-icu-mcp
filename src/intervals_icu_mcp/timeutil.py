"""Timezone-aware "now" for date-range defaults.

Hosted deployments run in UTC, but every "today" / "last N days" default must
follow the athlete's calendar day — otherwise ranges shift to tomorrow once the
athlete's local evening crosses UTC midnight. Configure INTERVALS_ICU_TIMEZONE
(IANA name, e.g. "America/New_York"); the default preserves UTC behavior.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .auth import ICUConfig

_UTC = ZoneInfo("UTC")


def local_now(config: ICUConfig) -> datetime:
    """Current time in the athlete's configured timezone (UTC on bad/missing tz)."""
    try:
        return datetime.now(ZoneInfo(config.intervals_icu_timezone))
    except Exception:
        return datetime.now(_UTC)
