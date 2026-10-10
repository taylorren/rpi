"""Versioned tunables for the RPI calculation.

Every number that affects the index lives here so any published RPI value can
be reproduced later. ``rpi.config.json`` overrides the defaults; absent keys
fall back to the values in this file.

Because these values shape the index, changing any of them makes historical
snapshots stale. ``config_version`` is stored alongside each snapshot so a
chart can tell when settings changed underneath it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

# Bump when the values below change in a way that alters the index.
#
# 2 -> 3 (2026-10-09): the index became a thermometer. The level is now
# base * exp(c * (S - b)) - a reading of the mood against normal - instead of an
# integral of the deviation, so snapshots from the two formulas are different
# series rather than different points of one.
#
# 3 -> 4 (2026-10-09): the mood gained an impact weighting, so the same stories
# now produce a different S(t) and therefore a different level.
#
# 4 -> 5 (2026-10-09): a story is counted once per outlet that covered it, not
# once in total. The index reads one report per (cluster, source) pair instead of
# one per cluster, so the same stories produce a different S(t) - 37% wider - and
# b was re-measured for it. See DEFAULT_ITEMS_PER_SOURCE and tools/scheme_ab.py.
#
# 5 -> 6 (2026-10-10): the impact weighting was raised from p = 2 to p = 3, so a
# consequential story carries more of the level and a big event moves the line
# further. It changes the units of S again - mean 0.3345 -> 0.9537, spread
# 0.5288 -> 1.0475 - so b and c were re-measured with it. See
# tools/weight_power_ab.py.
CONFIG_VERSION = 6

# How much a news item matters according to how far its effects reach.
DEFAULT_SCOPE_WEIGHTS: Dict[str, float] = {
    "global": 1.0,
    "major_regions": 0.7,
    "minor_regions": 0.4,
    "local": 0.2,
}

# Sensitivity of the thermometer: the level is base * exp(c * (S - b)), so c
# turns a mood deviation into a level move. It is not a rate - there is no
# per-day term any more - so it is set against the *observed* spread of the mood
# rather than against a full-scale day, and it has to be re-measured whenever
# that spread changes.
#
# The rule the value follows is c = ln(1.05) / sd(S): a one-standard-deviation
# mood is worth about 5% either side of 100. Re-measured whenever p or the
# aggregation changes the units of S:
#   p = 0   c 0.16    (unweighted mood)
#   p = 2   c 0.0243  (left in place when items_per_source widened the spread)
#   p = 3   c 0.0466  sd(S) 1.0475  (current)
#
# History: with items_per_source the mood widened - sd(S) 0.3935 -> 0.5404 - and
# c was deliberately LEFT ALONE at 0.0243, so the level swung about 37% further:
# one week 1.7% -> 2.5%, one month 5.8% -> 10.6%, median daily move 0.47% ->
# 0.60%. That was the direction section 8 of DESIGN-HISTORY.md asked for, where
# the chart was too smooth to read. Leaving it there also put c below its own
# rule - 1.3% per sd rather than 5% - so raising p to 3 re-measures c back to the
# rule rather than only re-centring: on p = 3 units a one-sd mood is 5% again,
# and the median daily move is about 2.2%. To hold the amplitude where p = 2
# left it instead, set c = 0.0123 (= 0.0243 * sd_old/sd_new); that keeps the
# chart's swing and changes only its shape. Measured with
# tools/weight_power_ab.py.
DEFAULT_C = 0.0466

# Sensitivity of the retired integrator. Kept because rpi.calibrate still
# expresses its drift budget through it - with k = 0.02 a full-scale day moved
# the index about 2%. The calculator no longer reads this: the index is a
# thermometer, so an error in b moves the level by a bounded factor rather than
# accumulating into a drift. See DEFAULT_C.
DEFAULT_K = 0.02

# Decay half-life for news relevance, in hours. Controls how quickly old news
# stops influencing the index.
DEFAULT_TAU_HOURS = 36.0

# Impact weighting: a story's influence is multiplied by |signed impact|^p, so a
# story twice as consequential counts 2^p times as much. p = 0 disables it and
# recovers the plain decay-weighted mean.
#
# It exists because a mean over hundreds of stories is dominated by the crowd:
# on the live corpus the largest story of a day holds 2.4% of the decay weight at
# p = 0, 8% at p = 2 and 17% at p = 3. Weighting by impact is also what the
# scoring rule already implies - a report that changed nothing should not count
# the same as one that changed a great deal - and it gives the level something to
# move on, because a neutral story now contributes exactly nothing instead of
# diluting the mood toward zero.
#
# Raising it widens the mood's spread, so b and c must be re-measured with it: at
# p = 2 the spread is about 4.6x the unweighted one, and at p = 3 it is about
# twice the p = 2 one again (sd(S) 0.5288 -> 1.0475 on the live corpus). Note
# what p does and does not buy: the biggest story's share of the window's weight
# roughly doubles (1.75% -> 3.37%), but so does the spread, so a single story's
# move relative to the day-to-day noise is about unchanged - p reshapes the
# distribution toward events, while c sets how far the line moves at all. Both
# were measured with tools/weight_power_ab.py.
DEFAULT_WEIGHT_POWER = 3.0

# One report per outlet per story, instead of one report per story.
#
# The index used to consume a single item per cluster - the representative - so a
# story covered by six outlets contributed one report's score and the other five
# were never analysed at all. Section 11 of DESIGN-HISTORY.md measured what that
# cost: 27% of sampled members would have flipped their story's sign, and the
# election rule (richest text wins) quietly suppressed the feeds that write short
# summaries - bbc-world held 9.2% of the pairs and only 2.2% of the index's
# weight, because its summaries are a fifth the length of Guardian's.
#
# With this on, a story counts once per outlet that covered it, so multiplicity
# needs no knob of its own: a story four outlets covered contributes four
# reports, while one outlet covering it four times still contributes one. Each
# report enters at its own published time, so a story's weight builds as coverage
# arrives - which is how section 5 says a story's persistence is meant to work,
# and what collapsing a cluster into one item deleted.
#
# It costs a scoring call per pair rather than per story (912 extra on the
# corpus it was measured on) and it changes the units of S, so b had to be
# re-measured with it. It does NOT repair a story the clustering fragmented: two
# clusters holding only negative reports still read negative.
DEFAULT_ITEMS_PER_SOURCE = True

# Reference level the index treats as "normal news". BOOTSTRAP VALUE.
# News media is structurally negative, so with b = 0 the index would decay
# forever. The default is 0 only until ``python -m rpi.calibrate --apply`` writes
# the measured mean of S(t) into ``rpi.config.json``, which is what the live site
# runs on; see that module for the drift budget the value is judged against.
# The frozen value should change only when the INSTRUMENT changes - a source
# added or dropped, or the scoring model or schema replaced - never to follow the
# news, because a frozen b cannot tell a darkening world from a hardening
# instrument, and re-tuning it would absorb the movement the index exists to show.
DEFAULT_BASELINE_B = 0.0

DEFAULT_INDEX_BASE = 100.0

DEFAULT_SNAPSHOT_MINUTES = 15

# --------------------------------------------------------------------------- #
# De-duplication
# --------------------------------------------------------------------------- #

# Only compare items whose event times are this close. Two reports of one event
# are essentially never days apart, so this bounds the candidate set cheaply.
DEFAULT_DEDUPE_WINDOW_HOURS = 72.0

# Title similarity above this merges locally, without spending a model call.
# Deliberately high: only near-identical headlines qualify.
DEFAULT_DEDUPE_AUTO_MERGE = 0.80

# Below this, a pair is not worth asking about. Deliberately LOW because the
# prefilter score does not predict the verdict: in testing, two pairs both
# scoring 0.43 produced opposite answers (P=0.001 and P=0.996), and a genuine
# match was a differently-worded headline. Being permissive here costs model
# calls; being strict silently loses duplicates.
DEFAULT_DEDUPE_REJECT_FLOOR = 0.25

# P(same event) at or above this merges. Measured verdicts were bimodal - all
# either above 0.96 or below 0.10 - so anything in that gap would do.
DEFAULT_DEDUPE_API_THRESHOLD = 0.50

# Most model calls spent per item before giving up and treating it as new.
DEFAULT_DEDUPE_MAX_API_PER_ITEM = 3

# Bump to invalidate cached pair verdicts when the prompt changes.
DEDUPE_PROMPT_VERSION = 1


@dataclass
class RpiConfig:
    """Tunables for the index calculation."""

    endpoint: str = "http://127.0.0.1:8765"
    base_level: float = DEFAULT_INDEX_BASE
    k: float = DEFAULT_K
    c: float = DEFAULT_C
    tau_hours: float = DEFAULT_TAU_HOURS
    weight_power: float = DEFAULT_WEIGHT_POWER
    items_per_source: bool = DEFAULT_ITEMS_PER_SOURCE
    baseline_b: float = DEFAULT_BASELINE_B
    snapshot_minutes: int = DEFAULT_SNAPSHOT_MINUTES
    scope_weights: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SCOPE_WEIGHTS))
    config_version: int = CONFIG_VERSION
    # False until baseline_b has been calibrated from real data.
    calibrated: bool = False

    dedupe_window_hours: float = DEFAULT_DEDUPE_WINDOW_HOURS
    dedupe_auto_merge: float = DEFAULT_DEDUPE_AUTO_MERGE
    dedupe_reject_floor: float = DEFAULT_DEDUPE_REJECT_FLOOR
    dedupe_api_threshold: float = DEFAULT_DEDUPE_API_THRESHOLD
    dedupe_max_api_per_item: int = DEFAULT_DEDUPE_MAX_API_PER_ITEM

    def scope_weight(self, scope: Optional[str]) -> float:
        """Weight for a scope label, tolerating unknown or missing values.

        An unrecognised scope falls back to the smallest weight rather than
        the largest, so a surprise label cannot silently inflate the index.
        """
        if not scope:
            return min(self.scope_weights.values(), default=0.2)
        return self.scope_weights.get(scope, min(self.scope_weights.values(), default=0.2))

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RpiConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs: Dict[str, Any] = {k: v for k, v in raw.items() if k in known}
        weights = kwargs.get("scope_weights")
        if isinstance(weights, Mapping):
            merged = dict(DEFAULT_SCOPE_WEIGHTS)
            merged.update({str(k): float(v) for k, v in weights.items()})
            kwargs["scope_weights"] = merged
        return cls(**kwargs)


def default_path() -> Path:
    return Path(__file__).resolve().parent.parent / "rpi.config.json"


def load(path: Optional[Path] = None) -> RpiConfig:
    """Load config, falling back to defaults when the file is absent."""
    target = path or default_path()
    if not target.exists():
        return RpiConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("cannot read config {}: {}".format(target, exc))
    if not isinstance(raw, Mapping):
        raise SystemExit("config {} must be a JSON object".format(target))
    return RpiConfig.from_mapping(raw)


def save(config: RpiConfig, path: Optional[Path] = None) -> Path:
    target = path or default_path()
    target.write_text(
        json.dumps(config.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return target
