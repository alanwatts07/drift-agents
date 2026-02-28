#!/usr/bin/env python3
"""Format Clawbr debates for judging analysis.

Data-only helper — fetches debate from Clawbr API and formats it into
structured text for analysis. No LLM calls.

Usage:
  python format_debate.py SLUG          # Print formatted debate transcript
  python format_debate.py --votable     # List debates open for voting

Reads CLAWBR_API_KEY from environment.
"""

import argparse
import json
import os
import sys

import requests

CLAWBR_API = "https://clawbr.org/api/v1"


def get_api_key():
    key = os.environ.get("CLAWBR_API_KEY")
    if not key:
        print("ERROR: CLAWBR_API_KEY not set in environment", file=sys.stderr)
        sys.exit(1)
    return key


def fetch_debate(slug: str) -> dict:
    """Fetch full debate data from Clawbr API."""
    r = requests.get(f"{CLAWBR_API}/debates/{slug}")
    r.raise_for_status()
    return r.json()


def format_debate(debate: dict) -> str:
    """Format debate data into structured text for judging analysis.

    Returns formatted text with topic, participants, rubric, and all posts.
    """
    topic = debate["topic"]
    challenger = debate["challenger"]
    opponent = debate["opponent"]
    rubric = debate.get("rubric", {})
    posts = debate.get("posts", [])
    series_ctx = debate.get("seriesContext")
    tournament_ctx = debate.get("tournamentContext")
    tournament_fmt = debate.get("tournamentFormat")

    all_posts = sorted(posts, key=lambda p: p["createdAt"])
    is_series = series_ctx is not None

    lines = []
    lines.append("=== DEBATE TOPIC ===")
    lines.append(topic)
    lines.append("")

    if series_ctx:
        lines.append("=== SERIES CONTEXT ===")
        lines.append(f"Best of {series_ctx['bestOf']}, currently Game {series_ctx['currentGame']}")
        lines.append(f"Score: PRO {series_ctx['proWins']} - CON {series_ctx['conWins']}")
        if series_ctx.get("sideNote"):
            lines.append(f"Note: {series_ctx['sideNote']}")
        lines.append("")

    if tournament_ctx:
        lines.append("=== TOURNAMENT CONTEXT ===")
        lines.append(f"{tournament_ctx.get('tournamentTitle', '')} — {tournament_ctx.get('roundLabel', '')} (Match {tournament_ctx.get('matchNumber', '?')})")
        lines.append("")

    if tournament_fmt:
        lines.append("=== FORMAT ===")
        if tournament_fmt.get("note"):
            lines.append(tournament_fmt["note"])
        lines.append("")

    lines.append("=== PARTICIPANTS ===")
    lines.append(f"CHALLENGER (PRO): @{challenger['name']} ({challenger['displayName']})")
    lines.append(f"OPPONENT (CON): @{opponent['name']} ({opponent['displayName']})")
    lines.append("")

    lines.append("=== JUDGING RUBRIC ===")
    if rubric and rubric.get("criteria"):
        for c in rubric["criteria"]:
            lines.append(f"- {c['name']} ({c['weight']}): {c['description']}")
    else:
        lines.append("- Clash & Rebuttal (40%): Did they directly respond to opponent's arguments? Dropped arguments count heavily against.")
        lines.append("- Evidence & Reasoning (25%): Were claims backed with evidence, examples, or logic?")
        lines.append("- Clarity (25%): Well-structured, concise, easy to follow?")
        lines.append("- Conduct (10%): Good faith, on-topic, no ad hominem or strawmanning?")
    if is_series:
        lines.append("- Originality (SERIES BONUS): In a series, recycled arguments from previous games should be penalized. Fresh angles, new evidence, and evolved positions are rewarded.")
    lines.append("")

    # Previous rounds for series debates
    if series_ctx and series_ctx.get("previousRounds"):
        for prev in series_ctx["previousRounds"]:
            game_num = prev.get("gameNumber", "?")
            winner_id = prev.get("winnerId")
            winner_name = prev.get("challengerName") if winner_id == debate.get("originalChallengerId") else prev.get("opponentName")
            lines.append(f"=== PREVIOUS GAME {game_num} (won by @{winner_name or 'unknown'}) ===")
            lines.append("")
            prev_posts = sorted(prev.get("posts", []), key=lambda p: p["postNumber"])
            for post in prev_posts:
                side_label = "PRO" if post["side"] == "challenger" else "CON"
                lines.append(f"--- Game {game_num} POST #{post['postNumber']} by @{post['authorName']} [{side_label}] ---")
                lines.append(post["content"])
                lines.append("")

    lines.append("=== CURRENT DEBATE POSTS (CHRONOLOGICAL ORDER) ===")
    lines.append("")

    for post in all_posts:
        side_label = "PRO" if post["side"] == "challenger" else "CON"
        author = post["authorName"]
        lines.append(f"--- POST #{post['postNumber']} by @{author} [{side_label}] ---")
        lines.append(post["content"])
        lines.append("")

    lines.append("=== END OF DEBATE ===")

    return "\n".join(lines)


def list_votable(api_key: str):
    """List debates open for voting that this agent hasn't voted on."""
    r = requests.get(f"{CLAWBR_API}/debates?limit=50")
    r.raise_for_status()
    data = r.json()
    debates = data.get("debates", data) if isinstance(data, dict) else data

    votable = [d for d in debates if d.get("votingStatus") == "open"]

    # Get our agent ID
    agent_id = None
    try:
        me_resp = requests.get(
            f"{CLAWBR_API}/agents/me",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        me_resp.raise_for_status()
        me = me_resp.json()
        agent_id = me.get("id") or me.get("agentId")
        if not agent_id:
            print(f"WARNING: Could not determine agent ID from /agents/me: {list(me.keys())}", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: /agents/me failed: {e} — vote status may be inaccurate", file=sys.stderr)

    for d in votable:
        full = fetch_debate(d["slug"])
        votes = full.get("votes", {}).get("details", [])
        already_voted = any(v.get("agentId") == agent_id for v in votes)
        status = "VOTED" if already_voted else "UNVOTED"
        print(f"[{status}] {d['slug']}: {d['topic'][:70]}")


def main():
    parser = argparse.ArgumentParser(description="Format Clawbr debates for judging")
    parser.add_argument("slug", nargs="?", help="Debate slug to format")
    parser.add_argument("--votable", action="store_true", help="List debates open for voting")
    args = parser.parse_args()

    if args.votable:
        list_votable(get_api_key())
    elif args.slug:
        debate = fetch_debate(args.slug)
        print(format_debate(debate))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
