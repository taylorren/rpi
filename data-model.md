# RPI Data Model

This document defines the first storage model for Renpin Index (RPI). It is written as a logical model first, and the initial implementation target is MySQL.

## Design Goals

- Preserve raw news inputs separately from normalized events.
- Keep event scoring auditable and recalculable.
- Support historical RPI charts.
- Allow future regional indexes, manual review, and scoring calibration.
- Avoid treating the AI scoring output as untraceable magic.

## Tables

## `sources`

Configured feeds, publishers, or news APIs.

Fields:

- `id`: Unique source identifier.
- `name`: Publisher or feed name.
- `feed_url`: RSS feed or API endpoint.
- `homepage_url`: Publisher homepage.
- `source_type`: `rss`, `api`, or `manual`.
- `category`: Optional source category, such as `global_news`, `science`, `finance`, or `regional`.
- `language`: Primary source language.
- `country`: Primary country or region, if applicable.
- `reliability_score`: Source reliability estimate from `0` to `1`.
- `bias_note`: Optional note about known framing or perspective.
- `fetch_interval_minutes`: How often the source should be checked.
- `last_fetched_at`: Last successful fetch time.
- `is_active`: Whether ingestion should use this source.
- `created_at`: Record creation time.
- `updated_at`: Last update time.

Recommended indexes:

- `is_active`
- `source_type`
- `last_fetched_at`

## `articles`

Raw imported article records.

Fields:

- `id`: Unique article identifier.
- `source_id`: Reference to `sources.id`.
- `url`: Canonical article URL.
- `external_id`: Optional source-provided article ID.
- `headline`: Original article headline.
- `author`: Optional author byline.
- `published_at`: Source publication time.
- `fetched_at`: Time the app imported the article.
- `language`: Article language.
- `raw_excerpt`: Feed excerpt or short description.
- `raw_content`: Full extracted article text when available.
- `content_hash`: Hash used for duplicate detection.
- `status`: `new`, `processed`, `ignored`, `failed`, or `duplicate`.
- `failure_reason`: Optional processing failure note.
- `created_at`: Record creation time.
- `updated_at`: Last update time.

Recommended indexes:

- `source_id`
- `published_at`
- `url`, unique when available
- `content_hash`
- `status`

## `events`

Normalized world events extracted from one or more articles.

Fields:

- `id`: Unique event identifier.
- `title`: Short neutral event title.
- `summary`: Neutral event summary.
- `category`: Main event category.
- `location_name`: Human-readable location.
- `country`: Country, if applicable.
- `region`: Region, if applicable.
- `latitude`: Optional latitude.
- `longitude`: Optional longitude.
- `scope`: `local`, `national`, `regional`, or `global`.
- `occurred_at`: When the event happened or started.
- `detected_at`: When the app first detected the event.
- `status`: `emerging`, `confirmed`, `updated`, `resolved`, or `disputed`.
- `source_count`: Number of linked source articles.
- `canonical_article_id`: Representative article for display.
- `created_at`: Record creation time.
- `updated_at`: Last update time.

Recommended indexes:

- `category`
- `status`
- `occurred_at`
- `detected_at`
- `scope`
- `country`

## `event_sources`

Join table connecting events to the articles used to identify or verify them.

Fields:

- `id`: Unique relation identifier.
- `event_id`: Reference to `events.id`.
- `article_id`: Reference to `articles.id`.
- `relation_type`: `primary`, `supporting`, `duplicate`, or `contradicting`.
- `confidence`: Confidence that the article belongs to the event cluster, from `0` to `1`.
- `created_at`: Record creation time.

Recommended indexes:

- `event_id`
- `article_id`
- Unique pair of `event_id` and `article_id`

## `event_scores`

Scoring result for an event. Multiple score records may exist for one event if the event is rescored after updates or calibration changes.

Fields:

- `id`: Unique score identifier.
- `event_id`: Reference to `events.id`.
- `scoring_version`: Version of the scoring rules or model contract.
- `model_name`: AI model or scoring engine used.
- `sentiment`: Direction of impact from `-1` to `1`.
- `severity`: Event seriousness from `0` to `1`.
- `scale`: Event reach from `0` to `1`.
- `novelty`: Newness of the information from `0` to `1`.
- `confidence`: Confidence in facts and interpretation from `0` to `1`.
- `category_weight`: Category multiplier.
- `duration_weight`: Duration multiplier.
- `impact_multiplier`: Global impact tuning value.
- `duration_hours`: Expected duration of impact.
- `decay_rate`: Rate used for time decay.
- `rpi_impact`: Final calculated event impact before decay.
- `rationale`: Plain-language explanation of the score.
- `uncertainty_notes`: Notes about missing, disputed, or weak evidence.
- `raw_agent_output`: Full structured scoring response for audit.
- `is_active`: Whether this score is currently used for RPI calculation.
- `created_at`: Record creation time.

Recommended indexes:

- `event_id`
- `scoring_version`
- `is_active`
- `created_at`

## `rpi_snapshots`

Historical RPI values used for charting and audit.

Fields:

- `id`: Unique snapshot identifier.
- `snapshot_at`: Time the RPI value was calculated.
- `index_scope`: `global` by default; may later support country or region.
- `baseline_rpi`: Baseline value used for the calculation.
- `current_rpi`: Final RPI value.
- `change_since_previous`: Difference from the previous snapshot.
- `positive_impact_sum`: Sum of active positive impacts.
- `negative_impact_sum`: Sum of active negative impacts.
- `active_event_count`: Number of events included in the calculation.
- `calculation_version`: Version of the RPI calculation rules.
- `created_at`: Record creation time.

Recommended indexes:

- `snapshot_at`
- `index_scope`
- Unique pair of `snapshot_at` and `index_scope`

## `rpi_snapshot_events`

Optional audit table connecting each snapshot to the event scores that contributed to it.

Fields:

- `id`: Unique relation identifier.
- `snapshot_id`: Reference to `rpi_snapshots.id`.
- `event_id`: Reference to `events.id`.
- `event_score_id`: Reference to `event_scores.id`.
- `current_impact`: Decayed impact used in this snapshot.
- `hours_since_detected`: Time value used for decay.
- `created_at`: Record creation time.

Recommended indexes:

- `snapshot_id`
- `event_id`
- `event_score_id`

## Relationships

- One `source` has many `articles`.
- One `article` may belong to many `events`.
- One `event` may use many `articles`.
- One `event` may have many `event_scores`, but only one should usually be active.
- One `rpi_snapshot` may include many scored events through `rpi_snapshot_events`.

## Data Lifecycle

1. A source is fetched on its configured interval.
2. New articles are inserted into `articles`.
3. Articles are processed into new or existing `events`.
4. Events receive records in `event_scores`.
5. The RPI calculator creates `rpi_snapshots`.
6. Snapshot contributors are stored in `rpi_snapshot_events`.

## Future Extensions

- `manual_reviews`: Human review and override history.
- `calibration_runs`: Records of scoring formula adjustments.
- `regions`: Stable regional taxonomy for regional RPI.
- `event_updates`: Timeline of changes to long-running events.
- `users`: App users, if review or personalization features are added.
