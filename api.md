# RPI API and Service Contract

This document defines the first service boundaries and API contracts for Renpin Index (RPI). The exact implementation may use HTTP endpoints, internal backend services, background jobs, or a mixture of these patterns.

## Design Goals

- Keep ingestion, extraction, scoring, and RPI calculation as separate responsibilities.
- Make every RPI movement explainable through stored events and scores.
- Allow background processing without blocking the web interface.
- Keep API responses structured and predictable.

## Service Modules

## Ingestion Service

Collects articles from configured sources.

Primary responsibilities:

- Fetch active sources.
- Parse RSS feed or API responses.
- Store new articles.
- Mark duplicate, failed, or ignored articles.

Suggested operations:

```text
runIngestion(source_id?)
fetchSource(source_id)
storeArticle(article_input)
```

## Extraction Service

Turns articles into normalized events.

Primary responsibilities:

- Decide whether an article describes a meaningful event.
- Match an article to an existing event when possible.
- Create new events when needed.
- Link articles to events.

Suggested operations:

```text
processArticle(article_id)
extractEventCandidate(article)
matchEvent(event_candidate)
createOrUpdateEvent(event_candidate)
```

## Scoring Service

Scores events and calculates event-level RPI impact.

Primary responsibilities:

- Build scoring input from event and source data.
- Call the AI scoring agent or deterministic scoring logic.
- Validate structured scoring output.
- Store score records.

Suggested operations:

```text
scoreEvent(event_id)
rescoreEvent(event_id, reason)
validateScore(score_output)
```

## RPI Calculation Service

Calculates current and historical RPI values.

Primary responsibilities:

- Load active event scores.
- Apply time decay.
- Sum event impacts.
- Store RPI snapshots.
- Store snapshot contributors for audit.

Suggested operations:

```text
calculateCurrentRpi(index_scope)
createRpiSnapshot(index_scope)
getSnapshotContributors(snapshot_id)
```

## Web API

The web API exposes data needed by the frontend.

## `GET /api/rpi/current`

Returns the latest RPI value.

Query parameters:

- `scope`: Optional index scope. Defaults to `global`.

Response:

```json
{
  "scope": "global",
  "current_rpi": 1004.25,
  "change_since_previous": 2.18,
  "snapshot_at": "2026-06-03T03:00:00Z",
  "active_event_count": 42,
  "positive_impact_sum": 12.7,
  "negative_impact_sum": -8.45
}
```

## `GET /api/rpi/history`

Returns chart data for RPI history.

Query parameters:

- `scope`: Optional index scope. Defaults to `global`.
- `from`: Optional start time.
- `to`: Optional end time.
- `interval`: Optional chart interval, such as `hour`, `day`, or `week`.

Response:

```json
{
  "scope": "global",
  "points": [
    {
      "snapshot_at": "2026-06-03T03:00:00Z",
      "current_rpi": 1004.25,
      "change_since_previous": 2.18
    }
  ]
}
```

## `GET /api/events`

Returns events for timeline and list views.

Query parameters:

- `status`: Optional event status filter.
- `category`: Optional category filter.
- `scope`: Optional event scope filter.
- `from`: Optional start time.
- `to`: Optional end time.
- `limit`: Maximum number of events.
- `cursor`: Pagination cursor.

Response:

```json
{
  "events": [
    {
      "id": "evt_123",
      "title": "Major earthquake affects coastal city",
      "summary": "A strong earthquake caused infrastructure damage and emergency response activity.",
      "category": "disaster",
      "location_name": "Example City",
      "scope": "regional",
      "occurred_at": "2026-06-03T01:20:00Z",
      "status": "confirmed",
      "source_count": 5,
      "current_impact": -3.42
    }
  ],
  "next_cursor": null
}
```

## `GET /api/events/{event_id}`

Returns event detail, sources, active score, and RPI rationale.

Response:

```json
{
  "event": {
    "id": "evt_123",
    "title": "Major earthquake affects coastal city",
    "summary": "A strong earthquake caused infrastructure damage and emergency response activity.",
    "category": "disaster",
    "location_name": "Example City",
    "scope": "regional",
    "occurred_at": "2026-06-03T01:20:00Z",
    "detected_at": "2026-06-03T01:35:00Z",
    "status": "confirmed"
  },
  "score": {
    "sentiment": -0.9,
    "severity": 0.8,
    "scale": 0.6,
    "novelty": 0.95,
    "confidence": 0.84,
    "rpi_impact": -4.13,
    "current_impact": -3.42,
    "rationale": "The event is negative because it caused harm, disruption, and emergency response needs."
  },
  "sources": [
    {
      "publisher": "Example News",
      "headline": "Earthquake strikes coastal city",
      "url": "https://example.com/article",
      "published_at": "2026-06-03T01:30:00Z",
      "reliability_score": 0.8
    }
  ]
}
```

## `GET /api/events/top-movers`

Returns the strongest positive and negative contributors to the current RPI.

Query parameters:

- `scope`: Optional index scope. Defaults to `global`.
- `limit`: Number of positive and negative events to return.

Response:

```json
{
  "positive": [
    {
      "event_id": "evt_456",
      "title": "Breakthrough treatment approved",
      "category": "health",
      "current_impact": 2.9
    }
  ],
  "negative": [
    {
      "event_id": "evt_123",
      "title": "Major earthquake affects coastal city",
      "category": "disaster",
      "current_impact": -3.42
    }
  ]
}
```

## `GET /api/sources`

Returns configured sources.

Response:

```json
{
  "sources": [
    {
      "id": "src_123",
      "name": "Example News",
      "source_type": "rss",
      "is_active": true,
      "reliability_score": 0.8,
      "last_fetched_at": "2026-06-03T02:55:00Z"
    }
  ]
}
```

## Admin and Internal Operations

These operations are useful for development and later admin tools. They should be protected before public deployment.

```text
POST /api/admin/ingestion/run
POST /api/admin/articles/{article_id}/process
POST /api/admin/events/{event_id}/score
POST /api/admin/events/{event_id}/rescore
POST /api/admin/rpi/snapshot
```

## Error Handling

API errors should use consistent fields:

```json
{
  "error": {
    "code": "score_validation_failed",
    "message": "The scoring output was missing required fields.",
    "details": {
      "missing_fields": ["severity", "confidence"]
    }
  }
}
```

## Background Jobs

Initial jobs:

- `fetch_sources`: Fetch active sources on their configured intervals.
- `process_articles`: Extract events from new articles.
- `score_events`: Score new or updated events.
- `create_rpi_snapshot`: Record the current RPI on a fixed interval.
- `cleanup_stale_items`: Mark stale processing records for review.

## API Principles

- Public-facing endpoints should return explanation-friendly data.
- Internal operations should preserve audit trails.
- Failed AI responses should be stored or logged for review, but not used directly in RPI calculation.
- The frontend should never need to recalculate RPI itself.
