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

from typing import Any, Dict, List, Mapping

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

# --------------------------------------------------------------------------- #
# Candidate impact anchors, under evaluation (tools/anchor_ab.py)
#
# The anchors above mix three dimensions into one scale:
#
#   * reach          - "affects few people", "changes conditions for many",
#                      "sustained regional impact"
#   * media attention - "widely reported", which is the strongest attractor for
#                      news of any kind and is why level 5 holds a third of the
#                      model's probability mass
#   * severity       - "severe", "grave", "world-historic"
#
# Reach is already asked separately as ``scope``, so requiring it inside
# ``impact`` has a concrete cost: a devastating local event cannot exceed about
# 3, while the top of the scale is reserved for descriptions no ordinary news
# item can match. Measured on the live corpus (2026-10-09): 96.8% of stories
# score 2-5, the model's entropy over the eleven levels is 1.40 bits - about 2.6
# effective levels - and nothing has ever reached 10.
#
# These anchors put every level on one axis, depth of consequence for those it
# touches, expressed as duration and reversibility, and say outright that reach
# lives elsewhere. Kept terse on purpose: the news text plus every field
# description has to fit the API's 2,048-token prompt limit.
IMPACT_ANCHORS_V2: Dict[str, str] = {
    "0": "nothing changes for anyone",
    "1": "a passing inconvenience, no lasting effect",
    "2": "a brief disruption, normal within days",
    "3": "a real but contained change, recovered within months",
    "4": "a material change in conditions, a year or more to recover, and not fully",
    "5": "a lasting change to lives or institutions, years to recover",
    "6": "a permanent change, the previous state cannot be restored",
    "7": "a change that redefines what is possible for those affected",
    "8": "a historic change, redirecting the future of a country or a whole field",
    "9": "among the largest events of the decade",
    "10": "world-historic, it changes the global order itself",
}

IMPACT_DESCRIPTION_V2 = (
    "Depth of this event's consequence for those it affects, good or bad: how "
    "long the effect lasts and whether it can be undone. Not how many people or "
    "how much of the world it reaches - reach is a separate field. "
    "0 = nothing changes, 10 = world-historic."
)


def impact_schema(anchors: Mapping[str, str] = IMPACT_ANCHORS_V2,
                  description: str = IMPACT_DESCRIPTION_V2) -> Dict[str, Dict[str, Any]]:
    """The analysis schema with alternative impact anchors.

    Returns a copy, so the same stored story can be scored under two rubrics and
    compared without touching the schema the pipeline ships with. Nothing here
    changes ``SCHEMA_VERSION``: adopting a rubric is a separate, deliberate step
    (it makes every stored score incomparable and re-queues the corpus).
    """
    out: Dict[str, Dict[str, Any]] = {
        name: dict(field) for name, field in ANALYSIS_SCHEMA.items()}
    impact = dict(out["impact"])
    impact["description"] = description
    impact["choice_descriptions"] = dict(anchors)
    out["impact"] = impact
    return out

# Plain-language rubric, used when validating the model by hand.
SCORING_NOTES = """
sentiment : positive / neutral / negative
impact    : 0-10 magnitude of consequence, independent of sign
scope     : global / major_regions / minor_regions / local
""".strip()
