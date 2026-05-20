"""Pin the subjective scale label mappings.

These tests exist because the intervals.icu scale direction is the OPPOSITE of
what most consumers assume (lower = better). If anyone "fixes" the maps to
match Garmin's display direction, every consumer of get_activity_details and
get_wellness_data will silently misread every reading. Lock the maps in.

Live verification: on 2026-05-19 activity i149654757 ("SST 2x25") was rated
"Strong" (4/5) in Garmin Connect and came back from intervals.icu as
``feel=2``. The mapping must label ``feel=2`` as "Strong" to match.
"""

from __future__ import annotations

import pytest

from intervals_icu_mcp.subjective_scales import (
    AFFECT_LABELS,
    FEEL_LABELS,
    FEEL_SCALE_NOTE,
    SEVERITY_LABELS,
    WELLNESS_SCALE_NOTE,
    feel_label,
    parse_feel_label,
    wellness_label,
)


class TestFeelLabels:
    """``feel`` is intervals.icu's 1-5 scale, INVERSE of Garmin's display."""

    def test_one_is_very_strong_best(self) -> None:
        assert FEEL_LABELS[1] == "Very Strong"

    def test_five_is_very_weak_worst(self) -> None:
        assert FEEL_LABELS[5] == "Very Weak"

    def test_two_is_strong_matches_live_observation(self) -> None:
        # 2026-05-19 i149654757: Garmin "Strong" → intervals.icu feel=2.
        # If this assertion ever flips to "Weak", the bug is back.
        assert FEEL_LABELS[2] == "Strong"

    def test_four_is_weak(self) -> None:
        assert FEEL_LABELS[4] == "Weak"

    def test_three_is_normal(self) -> None:
        assert FEEL_LABELS[3] == "Normal"

    def test_all_five_values_covered(self) -> None:
        assert set(FEEL_LABELS.keys()) == {1, 2, 3, 4, 5}

    def test_feel_label_helper(self) -> None:
        assert feel_label(1) == "Very Strong"
        assert feel_label(5) == "Very Weak"

    def test_feel_label_handles_none(self) -> None:
        assert feel_label(None) is None

    def test_feel_label_out_of_range_returns_none(self) -> None:
        assert feel_label(0) is None
        assert feel_label(6) is None

    def test_scale_note_mentions_inverse(self) -> None:
        assert "Inverse" in FEEL_SCALE_NOTE or "inverse" in FEEL_SCALE_NOTE


class TestParseFeelLabel:
    """The label-string input on update_activity is the safe write path."""

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("very_strong", 1),
            ("Very Strong", 1),
            ("very-strong", 1),
            ("strong", 2),
            ("Strong", 2),
            ("normal", 3),
            ("OK", 3),
            ("average", 3),
            ("weak", 4),
            ("bad", 4),
            ("very_weak", 5),
            ("Very Weak", 5),
            ("terrible", 5),
        ],
    )
    def test_aliases_round_to_correct_int(self, label: str, expected: int) -> None:
        assert parse_feel_label(label) == expected

    def test_unknown_label_returns_none(self) -> None:
        assert parse_feel_label("mediocre") is None

    def test_non_string_returns_none(self) -> None:
        # Defensive: the tool layer validates types, but the helper shouldn't
        # blow up if a caller fumbles. Signature is ``object`` so this is a
        # supported call, not a type violation.
        assert parse_feel_label(2) is None


class TestWellnessLabels:
    """Wellness fields share the 1=best convention but use 1-4 scales."""

    def test_severity_one_is_none_best(self) -> None:
        assert SEVERITY_LABELS[1] == "None"

    def test_severity_four_is_severe_worst(self) -> None:
        assert SEVERITY_LABELS[4] == "Severe"

    def test_affect_one_is_great_best(self) -> None:
        assert AFFECT_LABELS[1] == "Great"

    def test_affect_four_is_terrible_worst(self) -> None:
        assert AFFECT_LABELS[4] == "Terrible"

    def test_fatigue_uses_severity_scale(self) -> None:
        assert wellness_label("fatigue", 1) == "None"
        assert wellness_label("fatigue", 4) == "Severe"

    def test_soreness_uses_severity_scale(self) -> None:
        assert wellness_label("soreness", 2) == "Mild"

    def test_stress_uses_severity_scale(self) -> None:
        assert wellness_label("stress", 3) == "Moderate"

    def test_mood_uses_affect_scale(self) -> None:
        # The famous one: low mood number = good mood.
        assert wellness_label("mood", 1) == "Great"
        assert wellness_label("mood", 4) == "Terrible"

    def test_motivation_uses_affect_scale(self) -> None:
        assert wellness_label("motivation", 1) == "Great"

    def test_unknown_field_returns_none(self) -> None:
        assert wellness_label("readiness", 80) is None
        assert wellness_label("sleep_quality", 5) is None

    def test_handles_none_value(self) -> None:
        assert wellness_label("fatigue", None) is None

    def test_scale_note_states_one_is_best(self) -> None:
        assert "1 is always best" in WELLNESS_SCALE_NOTE
