"""Subjective rating scale labels and helpers.

Intervals.icu stores subjective ratings as small integers, but the scale
direction is the OPPOSITE of what most LLM consumers (and humans) assume:
on every subjective field, **lower numbers are better**. A "Strong" feel from
Garmin (FIT value 75/100, displayed as 4/5 in Garmin Connect) is synced as
intervals.icu ``feel=2``, not ``feel=4``.

The OpenAPI spec doesn't document the direction, so without a label any
consumer that sees ``feel: 2`` will reasonably guess "Weak (2 out of 5)".
This module exists so every tool that surfaces a subjective integer can emit
a human-readable label alongside it, and so the input side of update tools
can accept a label string instead of a direction-ambiguous integer.

Scales (verified for ``feel`` via live API on 2026-05-19; wellness scales
follow intervals.icu's web UI pictogram convention):

* Activity ``feel`` — 1-5, ``1 = Very Strong`` (best), ``5 = Very Weak`` (worst).
  Inverse of Garmin's 1-5 display scale.
* Wellness ``fatigue`` / ``soreness`` / ``stress`` — 1-4, ``1 = None`` (best),
  ``4 = Severe`` (worst).
* Wellness ``mood`` / ``motivation`` — 1-4, ``1 = Great`` (best),
  ``4 = Terrible`` (worst).

``sleep_quality`` and ``readiness`` are NOT inverted and are left unlabeled
here — ``sleep_quality`` syncs from Garmin (higher = better) and ``readiness``
is a 0-100 score where higher = better.
"""

from __future__ import annotations

FEEL_LABELS: dict[int, str] = {
    1: "Very Strong",
    2: "Strong",
    3: "Normal",
    4: "Weak",
    5: "Very Weak",
}

FEEL_SCALE_NOTE = (
    "intervals.icu feel: 1=Very Strong (best), 5=Very Weak (worst). "
    "Inverse of Garmin's display scale."
)

# Aliases accepted as the ``feel_label`` input to update_activity. Keys are
# normalized (lowercased, spaces/hyphens collapsed to underscores) before lookup.
_FEEL_INPUT_ALIASES: dict[str, int] = {
    "very_strong": 1,
    "strong": 2,
    "normal": 3,
    "average": 3,
    "ok": 3,
    "weak": 4,
    "bad": 4,
    "very_weak": 5,
    "terrible": 5,
}


# Wellness "bad-when-high" fields. UI pictograms go from "none" to "severe".
SEVERITY_LABELS: dict[int, str] = {
    1: "None",
    2: "Mild",
    3: "Moderate",
    4: "Severe",
}

# Wellness "good-when-low" affect fields. UI pictograms go from happiest to saddest.
AFFECT_LABELS: dict[int, str] = {
    1: "Great",
    2: "Good",
    3: "Poor",
    4: "Terrible",
}

WELLNESS_SCALE_NOTE = (
    "intervals.icu wellness scale: 1 is always best. "
    "fatigue/soreness/stress: 1=None, 4=Severe. "
    "mood/motivation: 1=Great, 4=Terrible."
)

_WELLNESS_LABEL_MAPS: dict[str, dict[int, str]] = {
    "fatigue": SEVERITY_LABELS,
    "soreness": SEVERITY_LABELS,
    "stress": SEVERITY_LABELS,
    "mood": AFFECT_LABELS,
    "motivation": AFFECT_LABELS,
}


def feel_label(value: int | None) -> str | None:
    """Return a human-readable label for an activity ``feel`` integer."""
    if value is None:
        return None
    return FEEL_LABELS.get(value)


def parse_feel_label(value: object) -> int | None:
    """Convert a feel label string to the intervals.icu integer.

    Accepts case-insensitive aliases like ``"Very Strong"``, ``"strong"``,
    ``"very-weak"``, ``"OK"``. Returns ``None`` for unknown strings AND for
    non-string inputs (the tool layer validates types; this helper just
    doesn't blow up). Caller raises a validation error on ``None``.
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    return _FEEL_INPUT_ALIASES.get(key)


def wellness_label(field: str, value: int | None) -> str | None:
    """Return a human-readable label for a wellness subjective integer.

    ``field`` is the field name (``fatigue``, ``soreness``, ``stress``,
    ``mood``, ``motivation``). Unknown fields return ``None``.
    """
    if value is None:
        return None
    labels = _WELLNESS_LABEL_MAPS.get(field)
    if labels is None:
        return None
    return labels.get(value)
