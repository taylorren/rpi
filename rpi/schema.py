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
# The candidate puts every level on one axis: depth of consequence for those it
# touches, in terms of how much changes and how long that lasts, with reach
# handed back to ``scope``. It has been through one A/B round - 100 stories,
# both evenly sampled and the 100 highest-impact stories:
#
#   * it fixes the tail, which is what an index needs in order to show a big
#     event: on the top-100 stories the spread went 0.71 to 1.47 and the levels
#     used went 4 to 9, where the anchors above cram 67 of 100 into level 6;
#   * it does not widen the general population (sd 1.05 to 1.03); there it
#     mostly shifts the distribution down, which re-calibrating b absorbs;
#   * its first revision had one systematic fault, and the two sentences in
#     IMPACT_DESCRIPTION_CANDIDATE exist to fix it: it judged the transience of
#     the reported act rather than the severity of the situation, demoting
#     "months of toxic haze" and scoring a UN condemnation of attacks on
#     civilians by the condemnation instead of by the attacks.
#
# A 24-story sample had suggested the spread doubled; 100 stories showed that
# was small-sample noise. Use --limit 100 for decisions.
#
# Kept terse on purpose: the news text plus every field description has to fit
# the API's 2,048-token prompt limit.
IMPACT_ANCHORS_CANDIDATE: Dict[str, str] = {
    "0": "nothing changes for anyone",
    "1": "a passing inconvenience, no lasting effect",
    "2": "a brief disruption, normal again within days",
    "3": "a real but contained change, undone within months",
    "4": "a material change in conditions, a year or more to undo",
    "5": "a lasting change to lives or institutions, years to undo, or an ongoing condition left unresolved",
    "6": "a permanent change, the previous state cannot be restored",
    "7": "a change that redefines what is possible for those affected",
    "8": "a historic change, redirecting the future of a country or a whole field",
    "9": "among the largest events of the decade",
    "10": "world-historic, it changes the global order itself",
}

IMPACT_DESCRIPTION_CANDIDATE = (
    "Depth of consequence for those it affects, good or bad: how much is "
    "changed, and how long that lasts. Judge the situation the story describes, "
    "not the act of reporting it - score what is condemned, forecast or "
    "analysed, not the condemnation, forecast or analysis. A condition that is "
    "still going counts as lasting, however it began. Not how many people or "
    "how much of the world it reaches; reach is a separate field. "
    "0 = nothing changes, 10 = world-historic."
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
