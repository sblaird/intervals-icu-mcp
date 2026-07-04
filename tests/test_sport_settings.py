"""Tests for the sport-settings model + tool (Issue #4: zones/type were null).

The upstream intervals.icu ``SportSettings`` payload has no scalar ``type`` field
(it has ``types``, a list), heart-rate threshold is ``lthr`` (not ``fthr``), pace
threshold is ``threshold_pace`` (not ``pace_threshold``), and it carries power/HR/
pace zone boundaries. The old model used the wrong field names, so every field
except ``ftp`` came back null and no zones were surfaced.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from httpx import Response

from intervals_icu_mcp.models import SportSettings
from intervals_icu_mcp.tools import sport_settings as ss_tool


def _monkeypatch_config(monkeypatch, mock_config):
    monkeypatch.setattr(ss_tool, "load_config", lambda: mock_config)
    monkeypatch.setattr(ss_tool, "validate_credentials", lambda _cfg: True)


# Realistic Ride sport-settings entry (field names mirror the intervals.icu API).
RIDE_SETTINGS = {
    "id": 10,
    "athlete_id": "i123456",
    "types": ["Ride", "VirtualRide", "GravelRide"],
    "ftp": 300,
    "indoor_ftp": 295,
    "w_prime": 20000,
    "p_max": 1100,
    "power_zones": [55, 75, 90, 105, 120, 150],
    "power_zone_names": ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6"],
    "lthr": 165,
    "max_hr": 190,
    "hr_zones": [81, 89, 94, 100, 103],
    "hr_zone_names": ["Z1", "Z2", "Z3", "Z4", "Z5"],
    "threshold_pace": 4.5,
    "pace_units": "MINS_KM",
    "pace_zones": [78.0, 88.0, 95.0, 105.0],
    "pace_zone_names": ["Easy", "Mod", "Thr", "VO2"],
}

# A Run entry with no power data — must still surface HR + pace.
RUN_SETTINGS = {
    "id": 11,
    "athlete_id": "i123456",
    "types": ["Run"],
    "lthr": 170,
    "max_hr": 192,
    "hr_zones": [80, 89, 96, 102],
    "threshold_pace": 3.6,
    "pace_units": "MINS_KM",
}


class TestSportSettingsModel:
    def test_parses_types_list_not_scalar_type(self):
        s = SportSettings(**RIDE_SETTINGS)
        assert s.types == ["Ride", "VirtualRide", "GravelRide"]

    def test_populates_hr_threshold_and_zones(self):
        s = SportSettings(**RIDE_SETTINGS)
        assert s.lthr == 165
        assert s.max_hr == 190
        assert s.hr_zones == [81, 89, 94, 100, 103]
        assert s.hr_zone_names == ["Z1", "Z2", "Z3", "Z4", "Z5"]

    def test_populates_power_zones(self):
        s = SportSettings(**RIDE_SETTINGS)
        assert s.ftp == 300
        assert s.indoor_ftp == 295
        assert s.power_zones == [55, 75, 90, 105, 120, 150]
        assert s.power_zone_names == ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6"]

    def test_populates_pace_threshold_and_units(self):
        s = SportSettings(**RIDE_SETTINGS)
        assert s.threshold_pace == 4.5
        assert s.pace_units == "MINS_KM"
        assert s.pace_zones == [78.0, 88.0, 95.0, 105.0]

    def test_ignores_unknown_fields(self):
        # Upstream sends dozens of fields we don't model; must not raise.
        # Build via **dict so the undeclared keys don't trip static type-checking.
        s = SportSettings(**{"id": 1, "types": ["Ride"], "some_future_field": "x", "other": True})
        assert s.id == 1


class TestGetSportSettingsTool:
    def _monkeypatch_config(self, monkeypatch, mock_config):
        monkeypatch.setattr(ss_tool, "load_config", lambda: mock_config)
        monkeypatch.setattr(ss_tool, "validate_credentials", lambda _cfg: True)

    async def test_surfaces_zones_and_types(self, mock_config, respx_mock, monkeypatch):
        self._monkeypatch_config(monkeypatch, mock_config)
        respx_mock.get("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=[RIDE_SETTINGS, RUN_SETTINGS])
        )

        result = await ss_tool.get_sport_settings(ctx=MagicMock())
        body = json.loads(result)
        entries = body["data"]["sport_settings"]
        assert len(entries) == 2

        ride = entries[0]
        assert ride["types"] == ["Ride", "VirtualRide", "GravelRide"]
        assert ride["ftp_watts"] == 300
        assert ride["indoor_ftp_watts"] == 295
        assert ride["power_zones"] == [55, 75, 90, 105, 120, 150]
        assert ride["power_zone_names"][0] == "Z1"
        assert ride["lthr_bpm"] == 165
        assert ride["max_hr_bpm"] == 190
        assert ride["hr_zones"] == [81, 89, 94, 100, 103]
        assert ride["threshold_pace"] == 4.5
        assert ride["pace_units"] == "MINS_KM"

    async def test_run_entry_without_power_still_surfaces_hr_and_pace(
        self, mock_config, respx_mock, monkeypatch
    ):
        self._monkeypatch_config(monkeypatch, mock_config)
        respx_mock.get("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=[RUN_SETTINGS])
        )

        result = await ss_tool.get_sport_settings(ctx=MagicMock())
        body = json.loads(result)
        run = body["data"]["sport_settings"][0]
        assert "ftp_watts" not in run
        assert run["lthr_bpm"] == 170
        assert run["max_hr_bpm"] == 192
        assert run["threshold_pace"] == 3.6

    async def test_zone_note_describes_units_accurately(self, mock_config, respx_mock, monkeypatch):
        """The zone_note must reflect real storage: power=%FTP, HR=absolute bpm, pace=m/s.

        Verified 2026-07-03 against athlete i29347: hr_zones are BPM (top == max_hr),
        NOT a percent of lthr as the first cut of the note wrongly claimed.
        """
        self._monkeypatch_config(monkeypatch, mock_config)
        respx_mock.get("/athlete/i123456/sport-settings").mock(
            return_value=Response(200, json=[RIDE_SETTINGS])
        )
        result = await ss_tool.get_sport_settings(ctx=MagicMock())
        note = json.loads(result)["analysis"]["zone_note"].lower()
        assert "bpm" in note
        assert "% of ftp" in note
        assert "% of lthr" not in note  # the old, incorrect claim
        assert "m/s" in note


class TestPaceConversion:
    """threshold_pace is stored in m/s; verify the human-unit conversions."""

    def test_min_per_km_to_mps(self):
        # 5:00/km -> 1000 / (5*60) = 3.333 m/s
        assert ss_tool._min_per_km_to_mps(5.0) == pytest.approx(3.3333, abs=1e-3)
        assert ss_tool._min_per_km_to_mps(4.5) == pytest.approx(1000 / (4.5 * 60))

    def test_min_per_100m_to_mps_matches_real_swim_entry(self):
        # 2:00/100m -> 100 / (2*60) = 0.8333 m/s, exactly athlete i29347's stored value.
        assert ss_tool._min_per_100m_to_mps(2.0) == pytest.approx(0.8333333, abs=1e-4)


class TestUpdateSportSettingsPaceWrite:
    async def test_pace_threshold_sent_as_mps_under_threshold_pace_key(
        self, mock_config, respx_mock, monkeypatch
    ):
        _monkeypatch_config(monkeypatch, mock_config)
        captured: dict = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json={"id": 5, "types": ["Run"], "threshold_pace": 3.3333})

        respx_mock.put("/athlete/i123456/sport-settings/5").mock(side_effect=handler)

        await ss_tool.update_sport_settings(sport_id=5, pace_threshold=5.0, ctx=MagicMock())
        assert captured["body"]["threshold_pace"] == pytest.approx(3.3333, abs=1e-3)
        assert "pace_threshold" not in captured["body"]  # legacy (ignored) key is gone
        # An update must not clobber the athlete's existing display preference.
        assert "pace_units" not in captured["body"]

    async def test_swim_threshold_sent_as_mps(self, mock_config, respx_mock, monkeypatch):
        _monkeypatch_config(monkeypatch, mock_config)
        captured: dict = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json={"id": 6, "types": ["Swim"]})

        respx_mock.put("/athlete/i123456/sport-settings/6").mock(side_effect=handler)

        await ss_tool.update_sport_settings(sport_id=6, swim_threshold=2.0, ctx=MagicMock())
        assert captured["body"]["threshold_pace"] == pytest.approx(0.8333333, abs=1e-4)

    async def test_both_pace_args_rejected(self, mock_config, respx_mock, monkeypatch):
        _monkeypatch_config(monkeypatch, mock_config)
        result = await ss_tool.update_sport_settings(
            sport_id=5, pace_threshold=5.0, swim_threshold=2.0, ctx=MagicMock()
        )
        body = json.loads(result)
        assert body["error"]["type"] == "validation_error"


class TestCreateSportSettingsPaceWrite:
    async def test_create_posts_then_puts_pace_in_mps(self, mock_config, respx_mock, monkeypatch):
        """Create silently drops threshold_pace, so the tool must POST then PUT it."""
        _monkeypatch_config(monkeypatch, mock_config)
        posts: list = []
        puts: list = []

        def post_handler(request):
            posts.append(json.loads(request.content))
            return Response(200, json={"id": 99, "types": ["Run"]})

        def put_handler(request):
            puts.append(json.loads(request.content))
            return Response(
                200, json={"id": 99, "types": ["Run"], "threshold_pace": puts[-1]["threshold_pace"]}
            )

        respx_mock.post("/athlete/i123456/sport-settings").mock(side_effect=post_handler)
        respx_mock.put("/athlete/i123456/sport-settings/99").mock(side_effect=put_handler)

        result = await ss_tool.create_sport_settings(
            sport_type="Run", pace_threshold=5.0, ctx=MagicMock()
        )
        body = json.loads(result)

        assert len(posts) == 1
        assert posts[0]["types"] == ["Run"]
        assert posts[0]["pace_units"] == "MINS_KM"  # sensible display default on create
        assert "threshold_pace" not in posts[0]  # dropped on create; sent via PUT
        assert len(puts) == 1
        assert puts[0]["threshold_pace"] == pytest.approx(3.3333, abs=1e-3)
        assert body["data"]["threshold_pace"] == pytest.approx(3.3333, abs=1e-3)

    async def test_create_without_pace_makes_no_put(self, mock_config, respx_mock, monkeypatch):
        _monkeypatch_config(monkeypatch, mock_config)
        posts: list = []
        puts: list = []

        def post_handler(request):
            posts.append(json.loads(request.content))
            return Response(200, json={"id": 100, "types": ["Ride"], "ftp": 300})

        def put_handler(request):
            puts.append(json.loads(request.content))
            return Response(200, json={"id": 100, "types": ["Ride"]})

        respx_mock.post("/athlete/i123456/sport-settings").mock(side_effect=post_handler)
        respx_mock.put("/athlete/i123456/sport-settings/100").mock(side_effect=put_handler)

        await ss_tool.create_sport_settings(sport_type="Ride", ftp=300, ctx=MagicMock())
        assert len(posts) == 1
        assert posts[0]["ftp"] == 300
        assert len(puts) == 0  # no pace -> no follow-up PUT
