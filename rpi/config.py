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
CONFIG_VERSION = 2

# How much a news item matters according to how far its effects reach.
DEFAULT_SCOPE_WEIGHTS: Dict[str, float] = {
    "global": 1.0,
    "major_regions": 0.7,
    "minor_regions": 0.4,
    "local": 0.2,
}

# Sensitivity: the fraction of the index moved by a full-scale day.
# With k = 0.02 a maximal +10 deviation moves the index about 2%, and a
# typical deviation of ~1 moves it about 0.2%.
DEFAULT_K = 0.02

# Decay half-life for news relevance, in hours. Controls how quickly old news
# stops influencing the index.
DEFAULT_TAU_HOURS = 36.0

# Reference level the index treats as "normal news". BOOTSTRAP VALUE.
# News media is structurally negative, so with b = 0 the index would decay
# forever. This is set to 0 provisionally and is frozen from accumulated
# analyses once roughly two weeks of data exist; see REQUIREMENTS.
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
    tau_hours: float = DEFAULT_TAU_HOURS
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
