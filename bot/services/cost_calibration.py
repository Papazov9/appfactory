"""Cost calibration.

The original estimator mapped every project to one of five fixed token tiers,
so the dollar estimate never reflected reality. Here we record the ACTUAL tokens
each build consumed, keyed by (stack, complexity), and feed a rolling average
back into the estimator. The first build of a given kind uses the tier default;
every build after that sharpens the estimate.

Stored as a small JSON file next to the project DB. Single-process asyncio bot,
tiny file → plain synchronous read/write is fine.
"""

from __future__ import annotations

import json
import logging

from bot.config import config

logger = logging.getLogger(__name__)

ALPHA = 0.35  # EMA weight given to each new sample


def _path():
    return config.DB_PATH.parent / "cost_calibration.json"


def _key(stack: str, complexity: str) -> str:
    return f"{stack}:{complexity}"


def load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save(data: dict):
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning(f"Could not save cost calibration: {e}")


def record(stack: str, complexity: str, input_tokens: int, output_tokens: int):
    """Fold an observed build's token usage into the rolling average."""
    if not stack or not complexity or input_tokens <= 0:
        return
    data = load()
    k = _key(stack, complexity)
    entry = data.get(k) or {"count": 0, "avg_in": 0.0, "avg_out": 0.0}
    if entry["count"] == 0:
        entry["avg_in"] = float(input_tokens)
        entry["avg_out"] = float(output_tokens)
    else:
        entry["avg_in"] = entry["avg_in"] * (1 - ALPHA) + input_tokens * ALPHA
        entry["avg_out"] = entry["avg_out"] * (1 - ALPHA) + output_tokens * ALPHA
    entry["count"] += 1
    data[k] = entry
    _save(data)


def predict(stack: str, complexity: str):
    """Return (avg_input, avg_output, sample_count) if we have data, else None."""
    entry = load().get(_key(stack, complexity))
    if entry and entry.get("count", 0) >= 1 and entry.get("avg_in", 0) > 0:
        return int(entry["avg_in"]), int(entry["avg_out"]), int(entry["count"])
    return None
