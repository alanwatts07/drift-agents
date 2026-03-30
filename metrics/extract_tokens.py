#!/usr/bin/env python3
"""
Post-session token extractor — reads JSONL session file, appends usage to daily CSV.

Usage:
    python3 metrics/extract_tokens.py <agent> <jsonl_path>

Called from run_agent.sh after each session completes.
"""

import csv
import fcntl
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

METRICS_DIR = Path(__file__).parent
SESSIONS_DIR = METRICS_DIR / "sessions"
ANOMALIES_LOG = METRICS_DIR / "anomalies.log"

AGENTS = ["max", "beth", "susan", "debater", "gerald", "private_aye"]

# Anomaly thresholds
HIGH_COST_MULTIPLIER = 2.0
LOW_COST_MULTIPLIER = 0.1
ROLLING_WINDOW = 20


def read_last_line(path: Path) -> str:
    """Read last non-empty line of a file efficiently (seek from end)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        # Read last 8KB — result lines are typically 1-3KB
        chunk_size = min(size, 8192)
        f.seek(size - chunk_size)
        data = f.read().decode("utf-8", errors="replace")
    lines = [l for l in data.strip().split("\n") if l.strip()]
    return lines[-1] if lines else ""


def extract_from_jsonl(agent: str, jsonl_path: Path) -> dict:
    """Extract token data from a JSONL session file."""
    row = {
        "timestamp": "",
        "agent": agent,
        "session_file": jsonl_path.name,
        "total_cost_usd": 0.0,
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "is_error": False,
        "models_used": "",
        "model_costs_json": "{}",
    }

    # Extract timestamp from filename: session_YYYYMMDD_HHMMSS.jsonl
    stem = jsonl_path.stem
    try:
        parts = stem.split("_")
        date_str = parts[1]
        time_str = parts[2] if len(parts) > 2 else "000000"
        row["timestamp"] = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}"
    except (IndexError, ValueError):
        row["timestamp"] = datetime.now().isoformat(timespec="seconds")

    # Check file size — tiny files are usually errors
    if jsonl_path.stat().st_size < 500:
        row["is_error"] = True
        return row

    last_line = read_last_line(jsonl_path)
    if not last_line:
        row["is_error"] = True
        return row

    try:
        data = json.loads(last_line)
    except json.JSONDecodeError:
        row["is_error"] = True
        return row

    if data.get("type") != "result":
        row["is_error"] = True
        return row

    # Extract usage data
    row["total_cost_usd"] = data.get("total_cost_usd", 0.0) or 0.0
    usage = data.get("usage", {})
    row["input_tokens"] = usage.get("input_tokens", 0) or 0
    row["cache_creation_tokens"] = usage.get("cache_creation_input_tokens", 0) or 0
    row["cache_read_tokens"] = usage.get("cache_read_input_tokens", 0) or 0
    row["output_tokens"] = usage.get("output_tokens", 0) or 0

    # Per-model breakdown
    model_usage = data.get("modelUsage", {})
    if model_usage:
        row["models_used"] = ";".join(model_usage.keys())
        row["model_costs_json"] = json.dumps(
            {k: {"cost": v.get("costUSD", 0), "in": v.get("inputTokens", 0),
                  "out": v.get("outputTokens", 0), "cache_read": v.get("cacheReadInputTokens", 0),
                  "cache_create": v.get("cacheCreationInputTokens", 0)}
             for k, v in model_usage.items()},
            separators=(",", ":"),
        )

    # Check for error in result
    if data.get("is_error"):
        row["is_error"] = True

    return row


def get_rolling_mean(agent: str, date_str: str) -> float | None:
    """Get rolling mean cost for an agent from recent session CSVs."""
    costs = []
    sessions_dir = SESSIONS_DIR

    # Check last 7 days of CSVs for this agent's sessions
    from datetime import timedelta
    try:
        base_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None

    for days_back in range(7):
        d = base_date - timedelta(days=days_back)
        csv_path = sessions_dir / f"{d.strftime('%Y-%m-%d')}.csv"
        if not csv_path.exists():
            continue
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if r.get("agent") == agent and r.get("is_error") != "True":
                        try:
                            costs.append(float(r["total_cost_usd"]))
                        except (ValueError, KeyError):
                            pass
        except Exception:
            continue
        if len(costs) >= ROLLING_WINDOW:
            break

    if len(costs) < 3:
        return None
    return statistics.mean(costs[:ROLLING_WINDOW])


def check_anomalies(row: dict) -> list[str]:
    """Check for anomalies in a session. Returns list of flags."""
    flags = []
    cost = row["total_cost_usd"]

    if row["is_error"]:
        flags.append("SESSION_FAILED")
        return flags

    if cost == 0:
        flags.append("ZERO_COST")
        return flags

    date_str = row["timestamp"][:10]
    mean = get_rolling_mean(row["agent"], date_str)
    if mean and mean > 0:
        ratio = cost / mean
        if ratio > HIGH_COST_MULTIPLIER:
            flags.append(f"HIGH_COST({ratio:.1f}x)")
        elif ratio < LOW_COST_MULTIPLIER:
            flags.append(f"LOW_COST({ratio:.2f}x)")

    return flags


CSV_FIELDS = [
    "timestamp", "agent", "session_file", "total_cost_usd",
    "input_tokens", "cache_creation_tokens", "cache_read_tokens",
    "output_tokens", "is_error", "models_used", "model_costs_json",
]


def append_to_csv(row: dict, date_str: str):
    """Append a row to the daily sessions CSV with file locking."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = SESSIONS_DIR / f"{date_str}.csv"
    is_new = not csv_path.exists()

    with open(csv_path, "a", newline="") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row[k] for k in CSV_FIELDS})
        fcntl.flock(f, fcntl.LOCK_UN)


def log_anomaly(row: dict, flags: list[str]):
    """Append anomaly to the anomalies log."""
    ts = row["timestamp"]
    agent = row["agent"]
    session = row["session_file"]
    cost = row["total_cost_usd"]
    flag_str = " ".join(flags)
    line = f"{ts} {agent} {session} cost=${cost:.4f} {flag_str}\n"

    with open(ANOMALIES_LOG, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <agent> <jsonl_path>", file=sys.stderr)
        sys.exit(1)

    agent = sys.argv[1]
    jsonl_path = Path(sys.argv[2])

    if not jsonl_path.exists():
        print(f"[metrics] File not found: {jsonl_path}", file=sys.stderr)
        sys.exit(1)

    row = extract_from_jsonl(agent, jsonl_path)
    date_str = row["timestamp"][:10]

    append_to_csv(row, date_str)

    flags = check_anomalies(row)
    if flags:
        log_anomaly(row, flags)
        print(f"[metrics] {agent} ${row['total_cost_usd']:.4f} ANOMALY: {' '.join(flags)}")
    else:
        print(f"[metrics] {agent} ${row['total_cost_usd']:.4f} {row['output_tokens']} output tokens")


if __name__ == "__main__":
    main()
