#!/usr/bin/env python3
"""
Demo API token tracker — logs per-call token usage to daily CSV.

Usage from demo_api/api.py:
    from metrics.api_tracker import log_api_call
    log_api_call(agent="max", input_tokens=100, output_tokens=500, model="claude-sonnet-4-5-20250929")
"""

import csv
import fcntl
import hashlib
from datetime import datetime
from pathlib import Path

METRICS_DIR = Path(__file__).parent
API_DIR = METRICS_DIR / "api"

# Approximate pricing per million tokens (as of March 2026)
MODEL_PRICING = {
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-opus-4-5-20251101": {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0}

CSV_FIELDS = [
    "timestamp", "agent", "endpoint", "model",
    "input_tokens", "output_tokens", "cost_usd", "latency_ms",
]


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def log_api_call(
    agent: str,
    input_tokens: int,
    output_tokens: int,
    model: str = "",
    endpoint: str = "/chat",
    latency_ms: int = 0,
):
    """Append one row to the daily API CSV."""
    API_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    csv_path = API_DIR / f"{date_str}.csv"
    is_new = not csv_path.exists()

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "endpoint": endpoint,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": f"{estimate_cost(model, input_tokens, output_tokens):.6f}",
        "latency_ms": latency_ms,
    }

    with open(csv_path, "a", newline="") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)
        fcntl.flock(f, fcntl.LOCK_UN)
