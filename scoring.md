# RPI Scoring Contract

This document defines how Renpin Index (RPI) events should be scored. The scoring model should be implemented as a separate module so it can be tested, calibrated, and replaced without rewriting ingestion or the web interface.

## Purpose

The scoring module estimates how much a normalized event changes RPI. It should produce structured, explainable output that can be stored, audited, and recalculated later.

## Inputs

The scoring module receives one normalized event plus its supporting sources.

Required event input:

```json
{
  "event_id": "evt_123",
  "title": "Major earthquake affects coastal city",
  "summary": "A strong earthquake caused infrastructure damage and emergency response activity.",
  "category": "disaster",
  "location_name": "Example City",
  "country": "Example Country",
  "scope": "regional",
  "occurred_at": "2026-06-03T01:20:00Z",
  "detected_at": "2026-06-03T01:35:00Z",
  "status": "confirmed",
  "source_count": 5
}
```

Required source input:

```json
{
  "sources": [
    {
      "publisher": "Example News",
      "headline": "Earthquake strikes coastal city",
      "url": "https://example.com/article",
      "published_at": "2026-06-03T01:30:00Z",
      "excerpt": "Emergency crews responded after a strong earthquake caused damage.",
      "reliability_score": 0.8,
      "bias_note": null
    }
  ]
}
```

## Score Dimensions

## `sentiment`

Direction of impact on RPI.

Range:

- `-1`: Extremely negative
- `0`: Neutral or unclear
- `1`: Extremely positive

Guidance:

- Use negative values for harm, instability, suffering, rights loss, ecological damage, or systemic risk.
- Use positive values for safety, health, peace, prosperity, discovery, cooperation, resilience, rights expansion, or ecological recovery.
- Use values near `0` when the event is mixed, unclear, or mostly procedural.

## `severity`

How intense or serious the event is.

Range: `0` to `1`

Guidance:

- Low severity: minor disruption, limited consequences, routine update.
- Medium severity: meaningful disruption, measurable harm or benefit.
- High severity: death, large-scale displacement, major breakthrough, systemic risk, or lasting global effect.

## `scale`

How many people, regions, institutions, or systems are affected.

Range: `0` to `1`

Guidance:

- Low scale: local or narrow effect.
- Medium scale: city, industry, national, or regional effect.
- High scale: multinational, global, or civilization-scale effect.

## `novelty`

How much of the event is new information.

Range: `0` to `1`

Guidance:

- Use high novelty for new events or major updates.
- Use lower novelty for repeated coverage, commentary, or small updates to an existing event.
- Use very low novelty for duplicate articles.

## `confidence`

Confidence in the facts and interpretation.

Range: `0` to `1`

Guidance:

- Increase confidence when multiple reliable sources agree.
- Decrease confidence when sources are sparse, disputed, vague, biased, or early.
- Low confidence events may still be stored but should have smaller RPI impact.

## `duration_hours`

Expected time period over which the event should affect RPI.

Guidance:

- Short-lived event: hours to a few days.
- Medium event: days to weeks.
- Long-running event: weeks to months.
- Structural event: months or longer.

## Weights

## Category Weight

`category_weight` adjusts the importance of event categories. The first version should keep weights conservative.

Initial defaults:

```json
{
  "disaster": 1.1,
  "conflict": 1.2,
  "health": 1.1,
  "economy": 1.0,
  "environment": 1.1,
  "science": 0.9,
  "technology": 0.9,
  "rights": 1.1,
  "culture": 0.7,
  "governance": 1.0
}
```

## Duration Weight

`duration_weight` increases the effect of events expected to matter longer.

Initial defaults:

```text
duration_weight = min(1.5, max(0.5, log10(duration_hours + 10) / 2))
```

## Impact Multiplier

`impact_multiplier` controls the overall sensitivity of the index.

Initial default:

```text
impact_multiplier = 10
```

This means a major event can move RPI visibly, while ordinary events have smaller effects.

## Formula

Initial event impact:

```text
rpi_impact = sentiment
  * severity
  * scale
  * novelty
  * confidence
  * category_weight
  * duration_weight
  * impact_multiplier
```

Current decayed impact:

```text
current_impact = rpi_impact * e^(-decay_rate * hours_since_detected)
```

Initial decay rate:

```text
decay_rate = 1 / max(duration_hours, 1)
```

This can be tuned later if event impact fades too quickly or too slowly.

## Required Output

The scoring agent must return valid structured JSON.

```json
{
  "event_id": "evt_123",
  "scoring_version": "rpi-score-v1",
  "category": "disaster",
  "sentiment": -0.9,
  "severity": 0.8,
  "scale": 0.6,
  "novelty": 0.95,
  "confidence": 0.84,
  "category_weight": 1.1,
  "duration_hours": 168,
  "duration_weight": 1.12,
  "decay_rate": 0.00595,
  "impact_multiplier": 10,
  "rpi_impact": -4.21,
  "rationale": "The event is negative because it caused harm, infrastructure damage, and emergency response needs. The scale is regional and confidence is high because several reliable sources report similar facts.",
  "uncertainty_notes": [
    "The final casualty and damage estimates may change as reporting develops."
  ]
}
```

## Validation Rules

The system should reject or flag scoring output when:

- Required fields are missing.
- Numeric values are outside their allowed ranges.
- `sentiment` is positive but the rationale describes only negative effects, or the reverse.
- `category` does not match a supported category.
- `confidence` is high despite sparse or disputed sources.
- `rpi_impact` does not match the formula within a small tolerance.
- The rationale is empty or does not explain the score.

## AI Scoring Prompt Shape

The scoring prompt should include:

- The definition of RPI.
- The event schema.
- The normalized event record.
- Source article summaries and reliability scores.
- Scoring dimensions and allowed ranges.
- Required JSON output format.
- Instruction to explain uncertainty and avoid unsupported assumptions.

The AI agent should be asked to score the event based only on supplied event and source data. If the data is insufficient, it should reduce `confidence` and explain the uncertainty.

## Human Review Rules

Future review tools should allow a human reviewer to:

- Mark a score as acceptable.
- Request a rescore.
- Override individual score dimensions.
- Add a review note.
- Disable an event score from active RPI calculation.

Manual overrides should create new score records instead of mutating old scores silently.

## Calibration Notes

The first scoring model is intentionally simple. Calibration should compare score behavior against historical examples and reviewer expectations.

Calibration questions:

- Are ordinary events moving RPI too much?
- Are rare major events moving RPI enough?
- Are positive events underweighted compared with negative events?
- Are some categories dominating the index?
- Does time decay feel too fast or too slow?

## Example Interpretation

A small local accident might receive:

```json
{
  "sentiment": -0.5,
  "severity": 0.2,
  "scale": 0.1,
  "novelty": 0.8,
  "confidence": 0.8
}
```

A major global medical breakthrough might receive:

```json
{
  "sentiment": 0.9,
  "severity": 0.7,
  "scale": 0.8,
  "novelty": 0.95,
  "confidence": 0.75
}
```

The exact impact should still be calculated by the scoring module, not by the frontend.
