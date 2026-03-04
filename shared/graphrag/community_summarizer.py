#!/usr/bin/env python3
"""
Community Summarizer — Phase 2 of GraphRAG pipeline.

Generates hierarchical LLM summaries for each detected community.
These summaries enable community-level retrieval: instead of matching
individual memories, queries can match against "this cluster is about
debate strategy and voting patterns" — dramatically improving recall.

Usage:
    python community_summarizer.py run                # Summarize all communities
    python community_summarizer.py run max             # Single agent
    python community_summarizer.py run --min-size 3    # Only communities with 3+ members
    python community_summarizer.py status              # Show summarization coverage
    python community_summarizer.py view <community_id> # View a community's summary

Architecture:
    1. Pull communities from Neo4j (skip singletons by default)
    2. For each community, pull member memory contents
    3. Send to LLM (Ollama) for structured summarization
    4. Write title + summary + key_themes back to Community nodes
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# Add graphrag dir to path for neo4j_adapter
sys.path.insert(0, str(Path(__file__).parent))
from neo4j_adapter import get_graph, close_driver

AGENTS = ['max', 'beth', 'susan', 'debater', 'gerald']

# LLM config — defaults to local Ollama
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
SUMMARIZE_MODEL = os.environ.get("OLLAMA_SUMMARIZE_MODEL", "qwen3:latest")


def extract_json(text: str) -> dict:
    """Extract a JSON object from LLM output that may contain extra text."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try markdown fenced block
    fenced = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Find first { ... } block (greedy from first { to last })
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Last resort: find any {...} substring
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON found in response ({len(text)} chars)")


def llm_summarize(memories_text: str, agent: str, community_id: str) -> dict:
    """
    Send memory cluster to LLM for summarization.
    Returns {"title": ..., "summary": ..., "key_themes": [...]}.
    """
    prompt = f"""You are analyzing a cluster of related memories from an agent named "{agent}" on a social debate platform.

Below are the memories that were grouped together by community detection (Leiden algorithm). They share structural connections in the agent's memory graph.

MEMORIES:
{memories_text}

Produce a JSON object with exactly these fields:
- "title": A short label for this cluster (5-10 words, like "Debate Strategy Against Beth" or "Crypto Fraud Pattern Analysis")
- "summary": 2-3 sentences describing what this cluster represents — what topic, activity, or pattern connects these memories
- "key_themes": A list of 3-5 theme keywords (e.g. ["voting", "debate tactics", "Beth rivalry", "infrastructure policy"])

Respond with ONLY the JSON object. No explanation, no markdown fencing."""

    url = f"{OLLAMA_HOST}/api/chat"
    headers = {"Content-Type": "application/json"}
    if OLLAMA_API_KEY:
        headers["X-API-Key"] = OLLAMA_API_KEY

    body = {
        "model": SUMMARIZE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {
            "num_predict": 512,
            "temperature": 0.3,
        },
    }

    # Disable thinking for qwen3
    if "qwen3" in SUMMARIZE_MODEL.lower():
        body["think"] = False

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=120)
        resp.raise_for_status()
        content = resp.json()["message"]["content"].strip()

        # Extract JSON from response — models often wrap it in text or markdown
        result = extract_json(content)

        # Validate required fields
        if not all(k in result for k in ("title", "summary", "key_themes")):
            raise ValueError(f"Missing fields: {result.keys()}")

        return result

    except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"    LLM error for {community_id}: {e}")
        return None


def pull_community_memories(graph, community_id: str) -> list:
    """Pull full memory content for all members of a community."""
    return graph.query("""
        MATCH (m:Memory)-[:BELONGS_TO]->(c:Community {id: $id})
        RETURN m.id AS id, m.content AS content, m.type AS type,
               m.importance AS importance, m.tags AS tags
        ORDER BY m.importance DESC
    """, {"id": community_id})


def format_memories_for_llm(memories: list, max_chars: int = 6000) -> str:
    """Format memories into a text block for the LLM, respecting token limits."""
    lines = []
    total = 0
    for i, m in enumerate(memories):
        content = (m.get("content") or "")[:300]
        tags = ", ".join(m.get("tags", [])[:3]) if m.get("tags") else ""
        line = f"[{i+1}] (imp={m.get('importance', 0.5):.2f}) {content}"
        if tags:
            line += f" [{tags}]"

        if total + len(line) > max_chars:
            lines.append(f"... and {len(memories) - i} more memories truncated")
            break
        lines.append(line)
        total += len(line)

    return "\n".join(lines)


def summarize_community(graph, community_id: str, agent: str) -> dict | None:
    """Summarize a single community and write results to Neo4j."""
    memories = pull_community_memories(graph, community_id)
    if not memories:
        return None

    memories_text = format_memories_for_llm(memories)
    result = llm_summarize(memories_text, agent, community_id)

    if result:
        # Write summary back to Neo4j
        graph.write("""
            MATCH (c:Community {id: $id})
            SET c.title = $title,
                c.summary = $summary,
                c.key_themes = $key_themes,
                c.summarized_at = datetime()
        """, {
            "id": community_id,
            "title": result["title"],
            "summary": result["summary"],
            "key_themes": result["key_themes"],
        })

    return result


def summarize_agent(agent: str, min_size: int = 2, force: bool = False):
    """Summarize all communities for an agent."""
    print(f"\n=== Summarizing communities: {agent} ===")
    start = time.time()
    graph = get_graph()

    # Pull communities that need summarization
    if force:
        communities = graph.query("""
            MATCH (c:Community {agent: $agent})
            WHERE c.size >= $min_size
            RETURN c.id AS id, c.size AS size
            ORDER BY c.size DESC
        """, {"agent": agent, "min_size": min_size})
    else:
        communities = graph.query("""
            MATCH (c:Community {agent: $agent})
            WHERE c.size >= $min_size AND c.summary IS NULL
            RETURN c.id AS id, c.size AS size
            ORDER BY c.size DESC
        """, {"agent": agent, "min_size": min_size})

    if not communities:
        print(f"  No communities to summarize (min_size={min_size})")
        return 0

    print(f"  {len(communities)} communities to summarize (min_size={min_size})")

    success = 0
    for i, comm in enumerate(communities):
        cid = comm["id"]
        size = comm["size"]
        result = summarize_community(graph, cid, agent)
        if result:
            success += 1
            title = result["title"][:60]
            print(f"  [{i+1}/{len(communities)}] {cid} ({size} memories) → \"{title}\"")
        else:
            print(f"  [{i+1}/{len(communities)}] {cid} ({size} memories) → FAILED")

        # Small delay to not hammer Ollama
        if i < len(communities) - 1:
            time.sleep(0.5)

    elapsed = time.time() - start
    print(f"  Done in {elapsed:.1f}s — {success}/{len(communities)} summarized")
    return success


def summarize_all(min_size: int = 2, force: bool = False):
    """Summarize communities for all agents."""
    total = 0
    for agent in AGENTS:
        try:
            n = summarize_agent(agent, min_size=min_size, force=force)
            total += n
        except Exception as e:
            print(f"  [{agent}] Error: {e}")

    print(f"\n=== Total: {total} communities summarized ===")


def show_status():
    """Show summarization coverage."""
    graph = get_graph()

    result = graph.query("""
        MATCH (c:Community)
        WHERE c.size >= 2
        WITH c.agent AS agent,
             count(c) AS total,
             sum(CASE WHEN c.summary IS NOT NULL THEN 1 ELSE 0 END) AS summarized,
             sum(c.size) AS total_memories
        RETURN agent, total, summarized, total_memories
        ORDER BY total DESC
    """)

    if not result:
        print("No communities found. Run community_detection.py first.")
        return

    print("=== Summarization Coverage (communities with 2+ members) ===")
    grand_total = 0
    grand_summarized = 0
    for row in result:
        total = row["total"]
        summarized = row["summarized"]
        grand_total += total
        grand_summarized += summarized
        pct = (summarized / total * 100) if total else 0
        print(f"  {row['agent']}: {summarized}/{total} summarized "
              f"({pct:.0f}%) — {row['total_memories']} memories covered")

    pct = (grand_summarized / grand_total * 100) if grand_total else 0
    print(f"\n  Overall: {grand_summarized}/{grand_total} ({pct:.0f}%)")

    # Show some example summaries
    examples = graph.query("""
        MATCH (c:Community)
        WHERE c.summary IS NOT NULL
        RETURN c.id AS id, c.agent AS agent, c.size AS size,
               c.title AS title, c.summary AS summary
        ORDER BY c.size DESC
        LIMIT 5
    """)
    if examples:
        print("\n  Sample summaries:")
        for row in examples:
            print(f"\n    {row['id']} ({row['size']} memories)")
            print(f"    Title: {row['title']}")
            summary = row['summary'][:200]
            print(f"    Summary: {summary}")


def view_community(community_id: str):
    """View a community's summary and members."""
    graph = get_graph()

    result = graph.query("""
        MATCH (c:Community {id: $id})
        RETURN c.title AS title, c.summary AS summary, c.key_themes AS key_themes,
               c.size AS size, c.agent AS agent, c.top_tags AS top_tags,
               c.avg_importance AS avg_importance, c.summarized_at AS summarized_at
    """, {"id": community_id})

    if not result:
        print(f"Community not found: {community_id}")
        return

    c = result[0]
    print(f"=== {community_id} ===")
    print(f"  Agent: {c['agent']}")
    print(f"  Size: {c['size']} memories")
    print(f"  Avg importance: {c['avg_importance']}")

    if c.get("title"):
        print(f"\n  Title: {c['title']}")
        print(f"  Summary: {c['summary']}")
        print(f"  Key themes: {c['key_themes']}")
        print(f"  Summarized at: {c['summarized_at']}")
    else:
        print("\n  [Not yet summarized]")

    print(f"  Top tags: {c['top_tags']}")

    # Member preview
    members = graph.query("""
        MATCH (m:Memory)-[:BELONGS_TO]->(c:Community {id: $id})
        RETURN m.content AS content, m.importance AS importance
        ORDER BY m.importance DESC
        LIMIT 5
    """, {"id": community_id})

    if members:
        print(f"\n  Top members:")
        for m in members:
            content = (m["content"] or "")[:120]
            print(f"    (imp={m['importance']:.2f}) {content}")


def main():
    parser = argparse.ArgumentParser(description="LLM summarization for memory communities")
    parser.add_argument("command", choices=["run", "status", "view"],
                        help="Command to execute")
    parser.add_argument("target", nargs="?", help="Agent name or community ID")
    parser.add_argument("--min-size", type=int, default=2,
                        help="Minimum community size to summarize (default: 2)")
    parser.add_argument("--force", action="store_true",
                        help="Re-summarize even if already done")
    args = parser.parse_args()

    if args.command == "run":
        if args.target and args.target in AGENTS:
            summarize_agent(args.target, min_size=args.min_size, force=args.force)
        else:
            summarize_all(min_size=args.min_size, force=args.force)
    elif args.command == "status":
        show_status()
    elif args.command == "view":
        if not args.target:
            print("Usage: community_summarizer.py view <community_id>")
            return
        view_community(args.target)

    close_driver()


if __name__ == "__main__":
    main()
