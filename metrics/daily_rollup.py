#!/usr/bin/env python3
"""
Daily rollup — aggregate session CSVs into summary + generate dashboard.

Usage:
    python3 metrics/daily_rollup.py                     # Roll up today
    python3 metrics/daily_rollup.py --date 2026-03-29   # Specific date
    python3 metrics/daily_rollup.py --backfill          # Process all historical JSONL files
    python3 metrics/daily_rollup.py --rebuild            # Rebuild summary + dashboard from all session CSVs
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

METRICS_DIR = Path(__file__).parent
SESSIONS_DIR = METRICS_DIR / "sessions"
DAILY_DIR = METRICS_DIR / "daily"
DASHBOARD_PATH = METRICS_DIR / "dashboard.html"
BASE_DIR = METRICS_DIR.parent

AGENTS = ["max", "beth", "susan", "debater", "gerald", "private_aye"]

# Import extract_tokens for backfill
sys.path.insert(0, str(METRICS_DIR))
from extract_tokens import extract_from_jsonl, append_to_csv, CSV_FIELDS


def backfill():
    """Process all historical JSONL files into session CSVs."""
    print("=== Backfilling from historical JSONL files ===")
    total = 0
    for agent in AGENTS:
        logs_dir = BASE_DIR / agent / "logs"
        if not logs_dir.exists():
            continue
        jsonl_files = sorted(logs_dir.glob("session_*.jsonl"))
        count = 0
        for jf in jsonl_files:
            try:
                row = extract_from_jsonl(agent, jf)
                date_str = row["timestamp"][:10]
                if not date_str or len(date_str) != 10:
                    continue
                append_to_csv(row, date_str)
                count += 1
            except Exception as e:
                print(f"  [{agent}] Error processing {jf.name}: {e}", file=sys.stderr)
        print(f"  [{agent}] {count} sessions extracted")
        total += count
    print(f"  Total: {total} sessions backfilled")
    return total


def read_sessions(date_str: str) -> list[dict]:
    """Read all sessions for a given date."""
    csv_path = SESSIONS_DIR / f"{date_str}.csv"
    if not csv_path.exists():
        return []
    with open(csv_path) as f:
        return list(csv.DictReader(f))


def read_all_sessions() -> dict[str, list[dict]]:
    """Read all session CSVs, grouped by date."""
    by_date = {}
    for csv_path in sorted(SESSIONS_DIR.glob("*.csv")):
        date_str = csv_path.stem
        with open(csv_path) as f:
            by_date[date_str] = list(csv.DictReader(f))
    return by_date


def aggregate_day(sessions: list[dict]) -> list[dict]:
    """Aggregate sessions into per-agent summary rows."""
    by_agent = defaultdict(lambda: {
        "session_count": 0, "total_cost_usd": 0.0,
        "total_input_tokens": 0, "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0, "total_output_tokens": 0,
        "error_count": 0,
    })

    for s in sessions:
        agent = s.get("agent", "unknown")
        d = by_agent[agent]
        d["session_count"] += 1
        d["total_cost_usd"] += float(s.get("total_cost_usd", 0) or 0)
        d["total_input_tokens"] += int(s.get("input_tokens", 0) or 0)
        d["total_cache_creation_tokens"] += int(s.get("cache_creation_tokens", 0) or 0)
        d["total_cache_read_tokens"] += int(s.get("cache_read_tokens", 0) or 0)
        d["total_output_tokens"] += int(s.get("output_tokens", 0) or 0)
        if s.get("is_error") == "True":
            d["error_count"] += 1

    rows = []
    for agent in sorted(by_agent.keys()):
        d = by_agent[agent]
        avg = d["total_cost_usd"] / d["session_count"] if d["session_count"] else 0
        rows.append({
            "agent": agent,
            "session_count": d["session_count"],
            "total_cost_usd": round(d["total_cost_usd"], 4),
            "avg_cost_per_session": round(avg, 4),
            "total_input_tokens": d["total_input_tokens"],
            "total_cache_creation_tokens": d["total_cache_creation_tokens"],
            "total_cache_read_tokens": d["total_cache_read_tokens"],
            "total_output_tokens": d["total_output_tokens"],
            "error_count": d["error_count"],
        })

    # ALL row
    totals = {
        "agent": "ALL",
        "session_count": sum(r["session_count"] for r in rows),
        "total_cost_usd": round(sum(r["total_cost_usd"] for r in rows), 4),
        "total_input_tokens": sum(r["total_input_tokens"] for r in rows),
        "total_cache_creation_tokens": sum(r["total_cache_creation_tokens"] for r in rows),
        "total_cache_read_tokens": sum(r["total_cache_read_tokens"] for r in rows),
        "total_output_tokens": sum(r["total_output_tokens"] for r in rows),
        "error_count": sum(r["error_count"] for r in rows),
    }
    totals["avg_cost_per_session"] = round(
        totals["total_cost_usd"] / totals["session_count"] if totals["session_count"] else 0, 4
    )
    rows.append(totals)
    return rows


SUMMARY_FIELDS = [
    "date", "agent", "session_count", "total_cost_usd", "avg_cost_per_session",
    "total_input_tokens", "total_cache_creation_tokens", "total_cache_read_tokens",
    "total_output_tokens", "error_count",
]


def write_summary(all_days: dict[str, list[dict]]):
    """Write or rebuild the summary CSV from aggregated data."""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = DAILY_DIR / "summary.csv"

    rows = []
    for date_str in sorted(all_days.keys()):
        day_rows = aggregate_day(all_days[date_str])
        for r in day_rows:
            r["date"] = date_str
            rows.append(r)

    with open(summary_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in SUMMARY_FIELDS})

    print(f"  Summary: {len(rows)} rows across {len(all_days)} days")


def generate_dashboard(all_days: dict[str, list[dict]]):
    """Generate a self-contained HTML dashboard with Chart.js."""
    # Prepare data for charts
    dates = sorted(all_days.keys())
    if not dates:
        print("  No data for dashboard")
        return

    # Per-agent daily costs
    agent_daily = {a: [] for a in AGENTS}
    daily_totals = []

    for d in dates:
        day_agg = {r["agent"]: r for r in aggregate_day(all_days[d])}
        total = 0
        for a in AGENTS:
            cost = day_agg.get(a, {}).get("total_cost_usd", 0)
            agent_daily[a].append(round(cost, 4))
            total += cost
        daily_totals.append(round(total, 4))

    # Cumulative spend
    cumulative = []
    running = 0
    for t in daily_totals:
        running += t
        cumulative.append(round(running, 2))

    # Token breakdown (daily totals)
    token_types = {"input": [], "cache_create": [], "cache_read": [], "output": []}
    for d in dates:
        day_agg = {r["agent"]: r for r in aggregate_day(all_days[d])}
        all_row = day_agg.get("ALL", {})
        token_types["input"].append(all_row.get("total_input_tokens", 0))
        token_types["cache_create"].append(all_row.get("total_cache_creation_tokens", 0))
        token_types["cache_read"].append(all_row.get("total_cache_read_tokens", 0))
        token_types["output"].append(all_row.get("total_output_tokens", 0))

    # Agent summary table (last 7 and 30 days)
    agent_table = []
    for a in AGENTS:
        costs_7d = agent_daily[a][-7:]
        costs_30d = agent_daily[a][-30:]
        sessions_7d = 0
        sessions_30d = 0
        for d in dates[-7:]:
            for s in all_days.get(d, []):
                if s.get("agent") == a:
                    sessions_7d += 1
        for d in dates[-30:]:
            for s in all_days.get(d, []):
                if s.get("agent") == a:
                    sessions_30d += 1
        agent_table.append({
            "name": a,
            "cost_7d": round(sum(costs_7d), 2),
            "cost_30d": round(sum(costs_30d), 2),
            "sessions_7d": sessions_7d,
            "sessions_30d": sessions_30d,
            "avg_cost": round(sum(costs_30d) / max(sessions_30d, 1), 4),
        })

    colors = {
        "max": "#3b82f6", "beth": "#ec4899", "susan": "#8b5cf6",
        "debater": "#f59e0b", "gerald": "#10b981", "private_aye": "#ef4444",
    }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Drift Agents — Token Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0a0a0f; color: #e5e5e5; font-family: 'Courier New', monospace; padding: 20px; }}
  h1 {{ color: #06b6d4; margin-bottom: 8px; }}
  .subtitle {{ color: #6b7280; margin-bottom: 24px; font-size: 14px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .card {{ background: #111118; border: 1px solid #1e293b; border-radius: 12px; padding: 20px; }}
  .card h2 {{ color: #06b6d4; font-size: 14px; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }}
  .full {{ grid-column: 1 / -1; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px; color: #6b7280; border-bottom: 1px solid #1e293b; }}
  td {{ padding: 8px; border-bottom: 1px solid #111; }}
  .cost {{ color: #10b981; font-weight: bold; }}
  .stat {{ display: inline-block; background: #1e293b; padding: 4px 10px; border-radius: 6px; margin: 4px; font-size: 13px; }}
  .stat-label {{ color: #6b7280; }}
  .stat-value {{ color: #06b6d4; font-weight: bold; }}
  canvas {{ max-height: 300px; }}
  @media (max-width: 768px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<h1>Drift Agents — Token Usage</h1>
<p class="subtitle">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} | {len(dates)} days tracked | {sum(daily_totals):.2f} USD total</p>

<div style="margin-bottom: 20px;">
  <span class="stat"><span class="stat-label">Total Spend:</span> <span class="stat-value">${sum(daily_totals):.2f}</span></span>
  <span class="stat"><span class="stat-label">Today:</span> <span class="stat-value">${daily_totals[-1] if daily_totals else 0:.2f}</span></span>
  <span class="stat"><span class="stat-label">7d Avg:</span> <span class="stat-value">${sum(daily_totals[-7:]) / min(len(daily_totals), 7):.2f}/day</span></span>
  <span class="stat"><span class="stat-label">Sessions:</span> <span class="stat-value">{sum(len(all_days[d]) for d in dates)}</span></span>
</div>

<div class="grid">
  <div class="card">
    <h2>Daily Cost by Agent</h2>
    <canvas id="dailyCost"></canvas>
  </div>
  <div class="card">
    <h2>Cumulative Spend</h2>
    <canvas id="cumulative"></canvas>
  </div>
  <div class="card full">
    <h2>Token Breakdown (Daily)</h2>
    <canvas id="tokenBreakdown"></canvas>
  </div>
  <div class="card full">
    <h2>Agent Summary</h2>
    <table>
      <tr><th>Agent</th><th>7d Cost</th><th>30d Cost</th><th>7d Sessions</th><th>30d Sessions</th><th>Avg $/Session</th></tr>
      {''.join(f'<tr><td>{a["name"]}</td><td class="cost">${a["cost_7d"]}</td><td class="cost">${a["cost_30d"]}</td><td>{a["sessions_7d"]}</td><td>{a["sessions_30d"]}</td><td>${a["avg_cost"]}</td></tr>' for a in agent_table)}
      <tr style="border-top:2px solid #06b6d4;font-weight:bold"><td>TOTAL</td><td class="cost">${sum(a["cost_7d"] for a in agent_table):.2f}</td><td class="cost">${sum(a["cost_30d"] for a in agent_table):.2f}</td><td>{sum(a["sessions_7d"] for a in agent_table)}</td><td>{sum(a["sessions_30d"] for a in agent_table)}</td><td>${sum(a["cost_30d"] for a in agent_table) / max(sum(a["sessions_30d"] for a in agent_table), 1):.4f}</td></tr>
    </table>
  </div>
</div>

<script>
const dates = {json.dumps(dates[-60:])};
const agentData = {json.dumps({a: agent_daily[a][-60:] for a in AGENTS})};
const cumData = {json.dumps(cumulative[-60:])};
const tokenData = {json.dumps({k: v[-60:] for k, v in token_types.items()})};
const colors = {json.dumps(colors)};

// Daily Cost stacked bar
new Chart(document.getElementById('dailyCost'), {{
  type: 'bar',
  data: {{
    labels: dates,
    datasets: Object.entries(agentData).map(([name, data]) => ({{
      label: name, data, backgroundColor: colors[name] + '99', borderColor: colors[name], borderWidth: 1,
    }}))
  }},
  options: {{
    responsive: true, plugins: {{ legend: {{ labels: {{ color: '#9ca3af' }} }} }},
    scales: {{
      x: {{ stacked: true, ticks: {{ color: '#6b7280', maxRotation: 45 }}, grid: {{ color: '#1e293b' }} }},
      y: {{ stacked: true, ticks: {{ color: '#6b7280', callback: v => '$' + v.toFixed(2) }}, grid: {{ color: '#1e293b' }} }}
    }}
  }}
}});

// Cumulative spend
new Chart(document.getElementById('cumulative'), {{
  type: 'line',
  data: {{
    labels: dates,
    datasets: [{{ label: 'Cumulative USD', data: cumData, borderColor: '#06b6d4', backgroundColor: '#06b6d422', fill: true, tension: 0.3 }}]
  }},
  options: {{
    responsive: true, plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: '#6b7280', maxRotation: 45 }}, grid: {{ color: '#1e293b' }} }},
      y: {{ ticks: {{ color: '#6b7280', callback: v => '$' + v }}, grid: {{ color: '#1e293b' }} }}
    }}
  }}
}});

// Token breakdown
new Chart(document.getElementById('tokenBreakdown'), {{
  type: 'bar',
  data: {{
    labels: dates,
    datasets: [
      {{ label: 'Output', data: tokenData.output, backgroundColor: '#ef444499' }},
      {{ label: 'Input', data: tokenData.input, backgroundColor: '#3b82f699' }},
      {{ label: 'Cache Create', data: tokenData.cache_create, backgroundColor: '#f59e0b99' }},
      {{ label: 'Cache Read', data: tokenData.cache_read, backgroundColor: '#10b98199' }},
    ]
  }},
  options: {{
    responsive: true, plugins: {{ legend: {{ labels: {{ color: '#9ca3af' }} }} }},
    scales: {{
      x: {{ stacked: true, ticks: {{ color: '#6b7280', maxRotation: 45 }}, grid: {{ color: '#1e293b' }} }},
      y: {{ stacked: true, ticks: {{ color: '#6b7280', callback: v => (v/1000).toFixed(0) + 'k' }}, grid: {{ color: '#1e293b' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""

    DASHBOARD_PATH.write_text(html)
    print(f"  Dashboard: {DASHBOARD_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Daily token usage rollup")
    parser.add_argument("--date", help="Date to roll up (YYYY-MM-DD, default: today)")
    parser.add_argument("--backfill", action="store_true", help="Process all historical JSONL files")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild summary + dashboard from all session CSVs")
    args = parser.parse_args()

    if args.backfill:
        backfill()

    print("=== Building summary + dashboard ===")
    all_days = read_all_sessions()

    if not all_days:
        print("  No session data found")
        return

    write_summary(all_days)
    generate_dashboard(all_days)
    print("  Done")


if __name__ == "__main__":
    main()
