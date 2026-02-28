#!/usr/bin/env python3
"""
Memory Dump — Inspect what's in each agent's brain.

Usage:
    python3 shared/memory_dump.py max                    # all memories
    python3 shared/memory_dump.py max --type core        # only core memories
    python3 shared/memory_dump.py max --tag lesson       # filter by tag
    python3 shared/memory_dump.py max --embeddings       # show embedding status
    python3 shared/memory_dump.py max --graph            # show co-occurrence edges
    python3 shared/memory_dump.py max --stats            # stats only
    python3 shared/memory_dump.py all --stats            # stats for all agents
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DRIFT_MEMORY_DIR = Path(__file__).parent / "drift-memory"
if str(DRIFT_MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(DRIFT_MEMORY_DIR))

AGENTS = ['max', 'beth', 'susan', 'debater']


def setup_env(agent: str):
    os.environ['DRIFT_DB_SCHEMA'] = agent
    os.environ.setdefault('DRIFT_DB_HOST', 'localhost')
    os.environ.setdefault('DRIFT_DB_PORT', '5433')
    os.environ.setdefault('DRIFT_DB_NAME', 'agent_memory')
    os.environ.setdefault('DRIFT_DB_USER', 'drift_admin')
    os.environ.setdefault('DRIFT_DB_PASSWORD', 'drift_agents_local_dev')
    os.environ.setdefault('OLLAMA_HOST', 'http://localhost:11434')
    os.environ.setdefault('OLLAMA_EMBED_MODEL', 'qwen3-embedding:0.6b')
    from db_adapter import reset_db
    reset_db()


def dump_memories(agent: str, type_filter: str = None, tag_filter: str = None,
                  show_embeddings: bool = False, show_graph: bool = False):
    setup_env(agent)
    import psycopg2.extras
    from db_adapter import get_db

    db = get_db()

    # Build query
    conditions = []
    params = []
    if type_filter:
        conditions.append("m.type = %s")
        params.append(type_filter)
    if tag_filter:
        conditions.append("%s = ANY(m.tags)")
        params.append(tag_filter)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with db._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Memories with embedding status
            cur.execute(f"""
                SELECT m.*,
                       e.memory_id IS NOT NULL as has_embedding,
                       e.indexed_at as embed_date
                FROM {db._table('memories')} m
                LEFT JOIN {db._table('text_embeddings')} e ON m.id = e.memory_id
                {where}
                ORDER BY m.created DESC
            """, params)
            rows = cur.fetchall()

    if not rows:
        print(f"  No memories found for {agent}" +
              (f" (type={type_filter})" if type_filter else "") +
              (f" (tag={tag_filter})" if tag_filter else ""))
        return

    print(f"\n{'='*70}")
    print(f"  {agent.upper()} — {len(rows)} memories")
    print(f"{'='*70}")

    for i, row in enumerate(rows):
        tags = row.get('tags') or []
        content = (row.get('content') or '')[:200].replace('\n', ' ')
        created = str(row.get('created', ''))[:19]
        recalled = row.get('last_recalled')
        recalled_str = str(recalled)[:19] if recalled else 'never'

        print(f"\n  [{i+1}] {row['id']}  ({row['type']})")
        print(f"      Created: {created}  |  Recalled: {recalled_str}  |  "
              f"recall_count: {row.get('recall_count', 0)}  |  "
              f"sessions_since: {row.get('sessions_since_recall', 0)}")
        print(f"      Emotional: {row.get('emotional_weight', 0):.2f}  |  "
              f"Importance: {row.get('importance', 0):.2f}  |  "
              f"Freshness: {row.get('freshness', 0):.2f}")
        if tags:
            print(f"      Tags: {', '.join(tags)}")
        if show_embeddings:
            emb_status = "embedded" if row.get('has_embedding') else "NO EMBEDDING"
            emb_date = str(row.get('embed_date', ''))[:19] if row.get('has_embedding') else ''
            print(f"      Embedding: {emb_status}  {emb_date}")
        print(f"      {content}")

    if show_graph:
        print(f"\n{'─'*70}")
        print(f"  CO-OCCURRENCE GRAPH")
        print(f"{'─'*70}")
        with db._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT c.memory_id, c.other_id, c.count,
                           m1.content as src_content,
                           m2.content as dst_content
                    FROM {db._table('co_occurrences')} c
                    LEFT JOIN {db._table('memories')} m1 ON m1.id = c.memory_id
                    LEFT JOIN {db._table('memories')} m2 ON m2.id = c.other_id
                    WHERE c.memory_id < c.other_id
                    ORDER BY c.count DESC
                    LIMIT 20
                """)
                edges = cur.fetchall()

        if edges:
            for edge in edges:
                src = (edge.get('src_content') or '')[:40].replace('\n', ' ')
                dst = (edge.get('dst_content') or '')[:40].replace('\n', ' ')
                print(f"  {edge['memory_id']} ←({edge['count']:.0f})→ {edge['other_id']}")
                print(f"    \"{src}...\"")
                print(f"    \"{dst}...\"")
        else:
            print("  No co-occurrence edges")


def dump_stats(agent: str):
    setup_env(agent)
    import psycopg2.extras
    from db_adapter import get_db

    db = get_db()

    with db._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) as total FROM {db._table('memories')}")
            total = cur.fetchone()['total']

            cur.execute(f"SELECT type, COUNT(*) as cnt FROM {db._table('memories')} GROUP BY type ORDER BY type")
            types = {r['type']: r['cnt'] for r in cur.fetchall()}

            cur.execute(f"SELECT COUNT(*) as cnt FROM {db._table('text_embeddings')}")
            embeddings = cur.fetchone()['cnt']

            cur.execute(f"SELECT COUNT(*) as cnt FROM {db._table('co_occurrences')}")
            cooccurrences = cur.fetchone()['cnt']

            cur.execute(f"SELECT COUNT(*) as cnt FROM {db._table('sessions')}")
            sessions = cur.fetchone()['cnt']

            cur.execute(f"SELECT COUNT(*) as cnt FROM {db._table('typed_edges')}")
            typed_edges = cur.fetchone()['cnt']

            cur.execute(f"SELECT COUNT(*) as cnt FROM {db._table('lessons')}")
            lessons = cur.fetchone()['cnt']

            cur.execute(f"""
                SELECT AVG(recall_count) as avg_recall,
                       MAX(recall_count) as max_recall,
                       AVG(emotional_weight) as avg_emotion,
                       AVG(importance) as avg_importance
                FROM {db._table('memories')}
            """)
            agg = cur.fetchone()

    embed_pct = f"{embeddings/total*100:.0f}%" if total > 0 else "N/A"

    print(f"  {agent.upper():10s} | {total:3d} memories ({types.get('core',0)}c/{types.get('active',0)}a/{types.get('archive',0)}ar) | "
          f"{embeddings:3d} embeddings ({embed_pct}) | "
          f"{sessions:2d} sessions | {cooccurrences:3d} edges | {typed_edges:2d} typed | {lessons:2d} lessons | "
          f"avg_recall={agg['avg_recall'] or 0:.1f} max_recall={agg['max_recall'] or 0}")


def main():
    parser = argparse.ArgumentParser(description='Dump agent memory contents')
    parser.add_argument('agent', help='Agent name or "all"')
    parser.add_argument('--type', dest='type_filter', choices=['core', 'active', 'archive'])
    parser.add_argument('--tag', dest='tag_filter')
    parser.add_argument('--embeddings', action='store_true', help='Show embedding status')
    parser.add_argument('--graph', action='store_true', help='Show co-occurrence graph')
    parser.add_argument('--stats', action='store_true', help='Stats summary only')

    args = parser.parse_args()

    agents = AGENTS if args.agent == 'all' else [args.agent]

    if args.stats:
        print(f"\n{'='*100}")
        print(f"  MEMORY STATS")
        print(f"{'='*100}")
        for agent in agents:
            try:
                dump_stats(agent)
            except Exception as e:
                print(f"  {agent.upper():10s} | ERROR: {e}")
        print()
        return

    for agent in agents:
        try:
            dump_memories(agent, args.type_filter, args.tag_filter,
                         args.embeddings, args.graph)
        except Exception as e:
            print(f"ERROR dumping {agent}: {e}")


if __name__ == '__main__':
    main()
