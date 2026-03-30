#!/usr/bin/env python3
"""
Extract topic-based edges from memory embeddings using pgvector cosine similarity.

For each agent:
  1. Find memory pairs with high cosine similarity (>= threshold)
  2. Insert SIMILAR_TO typed_edges in PostgreSQL
  3. Skips existing collaborator edges to avoid duplicates

Usage:
    python extract_topic_edges.py --all              # All agents
    python extract_topic_edges.py --agent max         # Single agent
    python extract_topic_edges.py --all --threshold 0.65  # Custom threshold
"""

import argparse
import os
import sys
import time

import psycopg2
import psycopg2.extras

AGENTS = ['max', 'beth', 'susan', 'debater', 'gerald', 'private_aye']

# Similarity threshold — 0.62 catches topically related memories without noise
DEFAULT_THRESHOLD = 0.62
# Max edges per agent to keep graph manageable
MAX_EDGES_PER_AGENT = 5000


def get_pg_conn():
    return psycopg2.connect(
        host=os.environ.get('DRIFT_DB_HOST', 'localhost'),
        port=int(os.environ.get('DRIFT_DB_PORT', '5433')),
        dbname=os.environ.get('DRIFT_DB_NAME', 'agent_memory'),
        user=os.environ.get('DRIFT_DB_USER', 'drift_admin'),
        password=os.environ.get('DRIFT_DB_PASSWORD', 'drift_agents_local_dev'),
    )


def extract_topic_edges(conn, agent: str, threshold: float = DEFAULT_THRESHOLD):
    """Find similar memory pairs and insert as SIMILAR_TO typed_edges."""
    cur = conn.cursor()

    # First, clear old SIMILAR_TO edges for this agent
    cur.execute(f"DELETE FROM {agent}.typed_edges WHERE relationship = 'similar_to'")
    deleted = cur.rowcount
    if deleted:
        print(f"  [{agent}] Cleared {deleted} old similar_to edges")

    # Find top similar pairs using pgvector cosine distance
    # 1 - cosine_distance = cosine_similarity
    # Only pair memories that are both in the memories table (active)
    print(f"  [{agent}] Computing embedding similarities (threshold={threshold})...")

    cur.execute(f"""
        INSERT INTO {agent}.typed_edges (source_id, target_id, relationship, confidence, evidence, auto_extracted)
        SELECT
            e1.memory_id AS source_id,
            e2.memory_id AS target_id,
            'similar_to' AS relationship,
            (1 - (e1.embedding <=> e2.embedding))::double precision AS confidence,
            'cosine similarity' AS evidence,
            true AS auto_extracted
        FROM {agent}.text_embeddings e1
        JOIN {agent}.text_embeddings e2
            ON e1.memory_id < e2.memory_id  -- avoid self-pairs and duplicates
        JOIN {agent}.memories m1 ON m1.id = e1.memory_id
        JOIN {agent}.memories m2 ON m2.id = e2.memory_id
        WHERE (1 - (e1.embedding <=> e2.embedding)) >= %s
        -- Exclude pairs that already have a collaborator edge
        AND NOT EXISTS (
            SELECT 1 FROM {agent}.typed_edges te
            WHERE te.source_id = e1.memory_id AND te.target_id = e2.memory_id
            AND te.relationship = 'collaborator'
        )
        ORDER BY (1 - (e1.embedding <=> e2.embedding)) DESC
        LIMIT %s
    """, (threshold, MAX_EDGES_PER_AGENT))

    inserted = cur.rowcount
    conn.commit()

    # Get distribution stats
    cur.execute(f"""
        SELECT
            count(*) AS total,
            avg(confidence)::numeric(4,3) AS avg_sim,
            min(confidence)::numeric(4,3) AS min_sim,
            max(confidence)::numeric(4,3) AS max_sim
        FROM {agent}.typed_edges
        WHERE relationship = 'similar_to'
    """)
    stats = cur.fetchone()
    print(f"  [{agent}] Inserted {inserted} similar_to edges "
          f"(avg={stats[1]}, min={stats[2]}, max={stats[3]})")

    cur.close()
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Extract topic-based edges from embeddings")
    parser.add_argument("--agent", help="Single agent to process")
    parser.add_argument("--all", action="store_true", help="Process all agents")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Cosine similarity threshold (default: {DEFAULT_THRESHOLD})")
    args = parser.parse_args()

    if not args.agent and not args.all:
        parser.error("Specify --agent <name> or --all")

    agents = AGENTS if args.all else [args.agent]
    conn = get_pg_conn()

    print(f"=== Extracting topic edges (threshold={args.threshold}) ===")
    start = time.time()
    total = 0

    for agent in agents:
        try:
            count = extract_topic_edges(conn, agent, args.threshold)
            total += count
        except Exception as e:
            print(f"  [{agent}] ERROR: {e}")
            conn.rollback()

    elapsed = time.time() - start
    print(f"\n=== Done: {total} topic edges across {len(agents)} agents in {elapsed:.1f}s ===")
    conn.close()


if __name__ == "__main__":
    main()
