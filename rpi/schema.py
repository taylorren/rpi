"""The questions we ask the analyser, and the version that identifies them.

Changing anything in this module changes the meaning of every score produced
afterwards. Old and new scores are **not** comparable, so ``SCHEMA_VERSION``
must be bumped whenever the prompts or choices change, and it is persisted
with every analysis row.

The API accepts a mapping of field name -> field definition, which means one
call can answer several questions at once. We exploit that: all three
dimensions are asked in a single request per news item.

Design notes
------------
* **Sentiment and magnitude are separate questions.** Asking a small quantized
  model for one signed score conflates "is this good or bad?" with "does it
  matter?", and it answers that combined question less reliably than two
  simpler ones.
* **Descriptions are kept terse** because the news text plus all field
  descriptions must fit inside the API's 2,048-token prompt limit.
* **Only ``impact`` is listed in ``SCORE_FIELDS``** so the API returns a
  probability-weighted ``expected_score`` for it. Sentiment and scope are
  categorical and are weighted by our own config instead.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Bump when the prompts, choices or choice descriptions below change.
SCHEMA_VERSION = 1

SENTIMENT_CHOICES = ["positive", "neutral", "negative"]

IMPACT_CHOICES = [str(level) for level in range(11)]

SCOPE_CHOICES = ["global", "major_regions", "minor_regions", "local"]

_IMPACT_ANCHORS: Dict[str, str] = {
    "0": "no meaningful consequence",
    "1": "negligible",
    "2": "very minor",
    "3": "minor, affects few people",
    "4": "moderate",
    "5": "notable, widely reported",
    "6": "significant",
    "7": "major, changes conditions for many",
    "8": "severe, sustained regional impact",
    "9": "grave, historic scale",
    "10": "world-historic, changes the global order",
}

# One request carries all three fields.
ANALYSIS_SCHEMA: Dict[str, Dict[str, Any]] = {
    "sentiment": {
        "type": "enum",
        "description": (
            "Overall tone of this news for the world at large: are its "
            "consequences positive, neutral, or negative?"
        ),
        "choices": SENTIMENT_CHOICES,
    },
    "impact": {
        "type": "enum",
        "description": (
            "Magnitude of this event's consequence for the world, regardless "
            "of whether it is good or bad. 0 = no consequence, "
            "10 = world-historic."
        ),
        "choices": IMPACT_CHOICES,
        "choice_descriptions": _IMPACT_ANCHORS,
    },
    "scope": {
        "type": "enum",
        "description": (
            "How wide is the reach of those affected: global, major_regions, "
            "minor_regions, or local?"
        ),
        "choices": SCOPE_CHOICES,
    },
}

# Only integer-valued enums yield an expected_score.
SCORE_FIELDS: List[str] = ["impact"]

# Plain-language rubric, used when validating the model by hand.
SCORING_NOTES = """
sentiment : positive / neutral / negative
impact    : 0-10 magnitude of consequence, independent of sign
scope     : global / major_regions / minor_regions / local
""".strip()
