# RPI System Design

This document describes the first buildable system design for Renpin Index (RPI). It should evolve alongside `requirements.md`.

## MVP Goal

The MVP should collect news from configured sources, extract meaningful world events, score those events, calculate the current RPI, and display the index with clear explanations for major movements.

## System Overview

RPI is made of five main parts:

1. News ingestion
2. Event extraction
3. Event scoring
4. RPI calculation
5. Web interface

The event schema and scoring model should remain separate modules so they can be expanded, tested, and calibrated independently.

## News Ingestion

The ingestion module collects articles from configured RSS feeds or news APIs.

Responsibilities:

- Fetch articles on a pre-defined interval.
- Store raw article metadata and content.
- Avoid importing the same article more than once.
- Track source reliability and publisher metadata.
- Mark failed or unavailable sources for later review.

Initial inputs:

- RSS feed URL
- publisher name
- source category
- source reliability score
- fetch interval

## Event Extraction

The event extraction module turns raw articles into structured events.

Responsibilities:

- Identify whether an article describes a meaningful event.
- Group related articles into the same event.
- Deduplicate repeated coverage.
- Extract event title, summary, category, location, and time.
- Track whether an event is emerging, confirmed, updated, resolved, or disputed.

The first version may use AI-assisted extraction, but the output should follow the event schema defined in `requirements.md`.

## Event Scoring

The scoring module estimates how much each event changes RPI.

Responsibilities:

- Score event sentiment, severity, scale, novelty, confidence, and duration.
- Apply category and duration weights.
- Calculate final `rpi_impact`.
- Generate a human-readable rationale.
- Store uncertainty notes when the score may be unreliable.

The scoring agent should return structured output. The system should reject or flag incomplete scoring responses.

## RPI Calculation

The RPI calculation module combines active event impacts into the current index value.

Responsibilities:

- Start from a baseline RPI value.
- Apply time decay to event impacts.
- Sum active event impacts.
- Store periodic RPI snapshots.
- Preserve the list of events that contributed to each movement.

Initial formula:

```text
current_rpi = baseline_rpi + sum(current_impact of active events)
```

The baseline can start at `1000` until a better calibration method is defined.

## Web Interface

The web app should make RPI feel like a live world index.

Initial views:

- Live RPI chart
- Current RPI value and recent change
- Top positive contributors
- Top negative contributors
- Event timeline
- Event detail view with scoring rationale
- Source list for each event

The interface should prioritize explanation. Users should be able to understand why the index moved.

## Storage Model

The first storage model should include:

- `sources`: configured feeds or publishers
- `articles`: raw imported article data
- `events`: normalized event records
- `event_sources`: relation between events and articles
- `event_scores`: scoring results and rationale
- `rpi_snapshots`: historical RPI values

## Suggested Tech Stack

The app should use a TypeScript-only implementation stack:

- Vue 3 with Vite for the web interface
- TypeScript for frontend, backend, workers, schemas, and scoring modules
- Fastify for the backend API service
- MySQL for persistent storage
- Prisma or Drizzle for database access
- A scheduled worker for ingestion and scoring
- Structured AI output for event extraction and scoring
- ECharts or Chart.js for RPI history

Python should not be used as the product backend. Any backend service, worker, schema module, scoring module, or database integration should be implemented in TypeScript.

## Open Questions

- Which news sources should be included first?
- Should RPI be global only, or should it also support regional indexes?
- How should category weights be calibrated?
- How should disputed or low-confidence events affect the index?
- Should users be able to manually review or override scores?
- How often should RPI snapshots be recorded?
