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
# Why the anchors above are the problem. They mix three dimensions into one
# scale:
#
#   * reach           - "affects few people", "changes conditions for many",
#                       "sustained regional impact"
#   * media attention - "widely reported", the strongest attractor for news of
#                       any kind
#   * severity        - "severe", "grave", "world-historic"
#
# Reach is already asked separately as ``scope``, so requiring it inside
# ``impact`` costs something concrete, and the measured result is a *bimodal*
# scale rather than a compressed one (live corpus, 2026-10-09): 96.8% of stories
# sit in 2-5 because "widely reported" catches all routine news, about 1% sit at
# 7-9.5 because "changes the global order" catches grand-sounding commentary,
# and level 6 - the middle - holds 2.2%. The eleven levels resolve to about 2.6.
#
# The anchors are the ones that measured best on the tail, and they put every
# level on one axis: depth of consequence for those it touches, in terms of how
# much changes and how long that lasts, with reach handed back to ``scope``.
# Measured over three A/B rounds (100 stories, evenly sampled and the 100
# highest-impact stories). Under the anchors above the tail is crammed - 67 of
# 100 top stories land on level 6, spread 0.71 over 4 levels - and the body sits
# at sd 0.99. With these anchors and the description below, the tail spreads to
# 1.52 over 10 levels and, uniquely among the wordings tried, the body moves
# too: sd 0.99 to 1.28. Two earlier descriptions left the body at 1.0, so it is
# the principle below, not the anchors, that reaches it.
#
# What the description says, and why. The index treats each report as its own
# event and delegates persistence to the news flow: if an event matters, later
# reports about it will arrive and carry their own scores, and guessing at
# relatedness here would mean modelling a world we do not model. So the score is
# what this report changed, as of this report - not the topic it discusses (an
# opinion piece changes nothing) and not the situation it refers back to. A
# condemnation is scored as a condemnation, because the attacks it condemns were
# their own earlier reports; a warning or a forecast has not changed anything
# yet, and if the storm kills, that will be reported and scored on its own.
#
# A 24-story sample once suggested the spread doubled; 100 stories showed that
# was small-sample noise. Use --limit 100 for decisions.
#
# Kept terse on purpose: the news text plus every field description has to fit
# the API's 2,048-token prompt limit.
IMPACT_ANCHORS_CANDIDATE: Dict[str, str] = {
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

IMPACT_DESCRIPTION_CANDIDATE = (
    "Score this report's own development: how much has changed, for those it "
    "affects, good or bad, as of this report. Not the topic it discusses, and "
    "not the situation it refers back to - those were, or will be, other "
    "reports, and each carries its own score. A warning, a forecast or a "
    "commentary has not changed anything yet. Not how many people it reaches; "
    "reach is a separate field. 0 = nothing changes, 10 = world-historic."
)


def impact_schema(anchors: Mapping[str, str] = IMPACT_ANCHORS_CANDIDATE,
                  description: str = IMPACT_DESCRIPTION_CANDIDATE
                  ) -> Dict[str, Dict[str, Any]]:
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
