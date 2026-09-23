This document serves as the ONLY API reference for an existing backend. 

DON'T CHANGE ANYTHING. 

## 3) Run the service (weights load once, ~30s)

```powershell
.venv\Scripts\python.exe .\nimble_serve.py --quant 4bit --port 8765
```

Then open **http://127.0.0.1:8765/** — a small form where you paste a context + schema and hit
**Score**. Keep that window open; the model stays in VRAM, so later calls are fast.

---

## Using the API

### `GET /health`
```powershell
curl.exe http://127.0.0.1:8765/health    # -> {"status":"ok", ...}
```

### `POST /score`
```json
{
  "context": "The store accepts returns within 30 days. This item was bought 12 days ago.",
  "schema": {
    "eligible": { "type": "boolean", "description": "Is this item within the store return window?" }
  },
  "score_fields": []
}
```
Same schema rules as the runners: `"boolean"` or `"enum"`, required non‑empty `description`,
enums need `"choices"` (1–26), optional `"choice_descriptions"`, and integer‑valued enums can be
listed in `score_fields` to also get an `expected_score`.

`curl`:
```powershell
curl.exe -X POST http://127.0.0.1:8765/score -H "Content-Type: application/json" -d "{\"context\":\"...\",\"schema\":{...}}"
```

Python:
```python
import json, urllib.request

def score(context, schema, score_fields=()):
    req = urllib.request.Request(
        "http://127.0.0.1:8765/score",
        data=json.dumps({"context": context, "schema": schema,
                         "score_fields": list(score_fields)}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

result = score(
    context="The store accepts returns within 30 days. This item was bought 12 days ago.",
    schema={"eligible": {"type": "boolean",
                         "description": "Is this item within the store return window?"}},
)
print(result["output"])                       # {'eligible': true}
print(result["fields"]["eligible"]["probabilities"])
```

Without the server, you can also run one‑shot with the CLI:
```powershell
.venv\Scripts\python.exe .\inference_quantized.py --quant 4bit --context "..." --schema '...'
```

## Question types and schema reference

schema maps field name to field definition. Each definition has a required
description plus a type that picks one of three question styles. These map to
TypeSafe primitives (https://docs.typesafe.ai/primitives):
boolean = Noul ("is this true?"), enum = Choice ("which of these?"),
integer enum = Score ("which level?"). Endpoint uses
Bespoke Nimble (https://github.com/bespokelabsai/nimble) format.

### 1) Noul / boolean - yes/no - type: boolean
A yes/no question grounded in the context. Ranks just true/false tokens.

```json
{ "eligible": { "type": "boolean", "description": "Is this item within the store return window?" } }
```
Context: "The store accepts returns within 30 days. This item was bought 12 days ago."
 -> eligible = true, P(true) ~= 0.9998.

### 2) Choice / single-select - type: enum
Present a fixed list of string choices (max 26) and ask which one applies.

```json
{ "sentiment": { "type": "enum", "description": "Overall sentiment toward the product.",
  "choices": ["positive", "neutral", "negative"] } }
```

### 3) Score / rubric - integer-valued enum + score_fields
Choices are integer strings (e.g. "0","1","2") representing rubric levels. List the
field name in score_fields to get a probability-weighted expected_score.

```json
{ "quality": { "type": "enum", "description": "Rate argument quality (0=none,1=weak,2=strong).",
  "choices": ["0","1","2"], "choice_descriptions": {"0":"no evidence","1":"weak","2":"strong"} } }
```
With "score_fields":["quality"]:
```json
"fields": { "quality": { "prediction": 2, "expected_score": 1.85 } }
```

Rule of thumb: boolean for yes/no, enum for a fixed menu, integer enum +
score_fields for a numeric rating. Max 26 choices per enum; prompts >2,048 tokens rejected.

---

## API usage summary & sample questions

**NOTE: These questions are just for reference. It may or may not have any linkage to our app to be developed. We can use to test the response, the response structure for further usage. **

The service is a simple HTTP server. Send any of the sample JSON payloads below
to `POST /score` — each contains a `context`, a `schema`, and (for scoring
questions) a `score_fields` list. This section is a compact copy-paste
reference for your other programs.

### Endpoint
```
POST http://127.0.0.1:8765/score
Content-Type: application/json
```

### Sample request bundle (machine-readable)

Save this block as `samples.json` and read it from your program:

```json
{
  "endpoint": "http://127.0.0.1:8765/score",
  "samples": [
    {
      "name": "boolean_return_window",
      "context": "The store accepts returns within 30 days. This item was bought 12 days ago.",
      "schema": {
        "eligible": {
          "type": "boolean",
          "description": "Is this item within the store return window?"
        }
      },
      "score_fields": []
    },
    {
      "name": "boolean_return_window_late",
      "context": "Returns must be requested within 30 days; the item was purchased 45 days ago.",
      "schema": {
        "eligible": {
          "type": "boolean",
          "description": "Is this item within the store return window?"
        }
      },
      "score_fields": []
    },
    {
      "name": "enum_sentiment",
      "context": "The laptop is fast and the screen is brilliant — best purchase in years!",
      "schema": {
        "sentiment": {
          "type": "enum",
          "description": "Overall sentiment toward the product.",
          "choices": ["positive", "neutral", "negative"]
        }
      },
      "score_fields": []
    },
    {
      "name": "enum_sentiment_mixed",
      "context": "The battery is okay but the keyboard is loud; overall it works fine.",
      "schema": {
        "sentiment": {
          "type": "enum",
          "description": "Overall sentiment toward the product.",
          "choices": ["positive", "neutral", "negative"]
        }
      },
      "score_fields": []
    },
    {
      "name": "score_quality_high",
      "context": "Solar panels cut CO₂ by 80% compared with coal; multiple peer-reviewed studies confirm this.",
      "schema": {
        "quality": {
          "type": "enum",
          "description": "Rate the argument's quality (0 = none, 1 = weak, 2 = strong).",
          "choices": ["0", "1", "2"],
          "choice_descriptions": {
            "0": "no supporting evidence",
            "1": "some weak evidence",
            "2": "strong, well-supported claim"
          }
        }
      },
      "score_fields": ["quality"]
    },
    {
      "name": "score_quality_weak",
      "context": "Solar panels might do something; I saw a meme about clean energy.",
      "schema": {
        "quality": {
          "type": "enum",
          "description": "Rate the argument's quality (0 = none, 1 = weak, 2 = strong).",
          "choices": ["0", "1", "2"]
        }
      },
      "score_fields": ["quality"]
    }
  ]
}
```