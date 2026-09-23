"""Client for the local scoring API.

Kept dependency-free (stdlib ``urllib`` only) so the analyser runs anywhere the
rest of the pipeline does.

The service is a single process with the quantized model resident in VRAM, so
requests are effectively serialised. Concurrency is therefore **not** offered
here: the caller runs items one at a time, and any parallelism should be added
only after measuring that the server actually benefits.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

DEFAULT_ENDPOINT = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 180.0
HEALTH_TIMEOUT = 10.0


class ApiError(RuntimeError):
    """Raised when the scoring service cannot be reached or rejects a request."""


def _post(endpoint: str, path: str, payload: Mapping[str, Any],
          timeout: float) -> Tuple[Dict[str, Any], float]:
    url = "{}/{}".format(endpoint.rstrip("/"), path.lstrip("/"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ApiError("HTTP {} from {}: {}".format(exc.code, url, detail)) from exc
    except urllib.error.URLError as exc:
        raise ApiError(
            "cannot reach {}: {} (is the server running?)".format(url, exc.reason)
        ) from exc

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    try:
        return json.loads(raw.decode("utf-8")), elapsed_ms
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("malformed JSON from {}: {}".format(url, exc)) from exc


def health(endpoint: str = DEFAULT_ENDPOINT) -> Dict[str, Any]:
    """Return the service health document, or raise ApiError."""
    url = "{}/health".format(endpoint.rstrip("/"))
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        raise ApiError("cannot reach {}: {}".format(url, exc)) from exc
    except json.JSONDecodeError as exc:
        raise ApiError("malformed JSON from {}: {}".format(url, exc)) from exc


def is_available(endpoint: str = DEFAULT_ENDPOINT) -> bool:
    try:
        health(endpoint)
    except ApiError:
        return False
    return True


def score(
    context: str,
    schema: Mapping[str, Any],
    score_fields: Iterable[str] = (),
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Ask the analyser to fill in ``schema`` for ``context``.

    Returns the raw response document, which includes ``output`` (the filled
    fields) and ``fields`` (per-field detail such as ``probabilities`` and,
    for integer enums listed in ``score_fields``, ``expected_score``).
    """
    payload = {
        "context": context,
        "schema": dict(schema),
        "score_fields": list(score_fields),
    }
    result, _ = _post(endpoint, "score", payload, timeout)
    return result


def score_timed(
    context: str,
    schema: Mapping[str, Any],
    score_fields: Iterable[str] = (),
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[Dict[str, Any], float]:
    """As :func:`score`, but also returns the round-trip latency in milliseconds."""
    payload = {
        "context": context,
        "schema": dict(schema),
        "score_fields": list(score_fields),
    }
    return _post(endpoint, "score", payload, timeout)


def compact_probabilities(
    result: Mapping[str, Any], fields: Iterable[str]
) -> Dict[str, Dict[str, float]]:
    """Pull ``fields.<name>.probabilities`` out of a response, if present.

    Used for the soft-output record: the full distribution is more useful than
    the argmax alone, because it exposes model confidence and lets a later
    revision re-weight without re-querying the model.
    """
    out: Dict[str, Dict[str, float]] = {}
    detail = result.get("fields") or {}
    if not isinstance(detail, Mapping):
        return out
    for name in fields:
        entry = detail.get(name)
        if isinstance(entry, Mapping) and isinstance(entry.get("probabilities"), Mapping):
            out[name] = {
                str(k): float(v)
                for k, v in entry["probabilities"].items()
                if isinstance(v, (int, float))
            }
    return out


def expected_scores(result: Mapping[str, Any]) -> Dict[str, float]:
    """Pull every ``fields.<name>.expected_score`` out of a response."""
    out: Dict[str, float] = {}
    detail = result.get("fields") or {}
    if not isinstance(detail, Mapping):
        return out
    for name, entry in detail.items():
        if isinstance(entry, Mapping) and isinstance(entry.get("expected_score"), (int, float)):
            out[str(name)] = float(entry["expected_score"])
    return out
