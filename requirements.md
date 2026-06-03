# Renpin Index (RPI)

This app tracks RPI in a pre-defined interval and uses an algorithm to calculate RPI.

It also displays real time RPI like a stock index, with major events that affect the RPI (and why). 

## Definition

### Renpin

Renpin is a Chinese word which can be interpreted as the "The quality (品, pin）of a person (人，ren）". In most cases, it is used to describe how good a person is. 

I borrow and extend this concept to describe how good/bad the world is becoming. 

### Renpin Index (RPI)

Thus, RPI is an index that is calculated to measure how good/bad the world is. Currently, I don't set an upper limit to it. 

## Key challenges

### Source of "Truth"

I am thinking using news RSS to extract news contents. Thus, I need a neutral and reliable news source for this purpose. 

### Calculation of RPI

News can have, in general and common sense, a positive/negative/neutral effect, thus changing the RPI calculation for that particular event. 

Also, different pieces of news have different weights. For example, and no offense, an earthquake in the middle of nowhere is less significant than one in the center of a big city.

So, we must consider the weightage of any event, and thus the final RPI impact.

Also, in calculating RPI, a fresh model trained from scratch will take too much time to train. So we should rely on a reliable and neutral AI agent for feedback. 

After getting that feedback for a particular event, we may need to further adjust the weightage for a certain period of time. 

## Event Schema

An event is the basic unit that changes RPI. A single event may be created from one article or from a cluster of related articles.

The event schema and scoring model should be implemented as separate modules so they can be expanded, tested, and calibrated independently over time.

### Core fields

- `id`: Unique event identifier.
- `title`: Short human-readable event title.
- `summary`: Neutral summary of what happened.
- `category`: Main event category, such as `disaster`, `conflict`, `health`, `economy`, `environment`, `science`, `technology`, `rights`, `culture`, or `governance`.
- `location`: Country, region, city, or global scope.
- `occurred_at`: When the event happened or started.
- `detected_at`: When the app first detected the event.
- `sources`: List of source articles used to identify and verify the event.
- `source_count`: Number of independent sources in the event cluster.
- `status`: `emerging`, `confirmed`, `updated`, `resolved`, or `disputed`.

### Scoring fields

- `sentiment`: Direction of impact on RPI. Range: `-1` to `1`, where negative events reduce RPI and positive events increase it.
- `severity`: How intense the event is. Range: `0` to `1`.
- `scale`: How many people, systems, or regions are affected. Range: `0` to `1`.
- `novelty`: How much of the event is new information rather than repeated coverage. Range: `0` to `1`.
- `confidence`: How confident the system is in the event interpretation. Range: `0` to `1`.
- `duration`: Expected persistence of the impact, in hours or days.
- `decay_rate`: How quickly the event impact fades.
- `rpi_impact`: Final calculated impact on RPI.
- `rationale`: Plain-language explanation of why this score was assigned.

### Source fields

Each source article should include:

- `url`: Article URL.
- `publisher`: Publisher name.
- `published_at`: Article publication time.
- `headline`: Original headline.
- `excerpt`: Short excerpt or summary.
- `reliability_score`: Source reliability estimate. Range: `0` to `1`.
- `bias_note`: Optional note if the source framing may affect interpretation.

## Scoring Model

The scoring model should estimate how much each event changes RPI. The first version should be simple, explainable, and adjustable.

### Score dimensions

Each event receives these normalized scores:

- `sentiment`: Whether the event is good or bad for the world.
- `severity`: The seriousness of the event.
- `scale`: The size of the affected population, geography, or system.
- `novelty`: Whether this is a genuinely new event or only repeated reporting.
- `confidence`: How much trust we have in the event facts and interpretation.
- `category_weight`: A configurable multiplier for the event category.
- `duration_weight`: A multiplier based on how long the event is expected to matter.

### Initial formula

The first draft formula:

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

`impact_multiplier` is a global tuning value that controls how strongly events move the index.

### Time decay

Most events should lose influence over time unless new information reinforces them.

```text
current_impact = rpi_impact * e^(-decay_rate * hours_since_detected)
```

Long-running events, such as wars, pandemics, economic crises, or major scientific breakthroughs, should decay more slowly than short-lived events.

### Index update

RPI should start from a baseline value, such as `1000`.

```text
current_rpi = baseline_rpi + sum(current_impact of active events)
```

The app should keep the raw event scores, the final impact, and the rationale so every RPI movement can be explained later.

### AI scoring agent

The AI agent should return structured scoring output for every event:

- event summary
- category
- sentiment score
- severity score
- scale score
- novelty score
- confidence score
- estimated duration
- rationale
- uncertainty notes

The AI agent should not be the only source of truth. Its output should be constrained by source data, scoring rules, and later calibration.

## Other considerations

This should be a web-based app using a modern tech stack and UI components. 


 
