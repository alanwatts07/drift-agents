#!/usr/bin/env python3
"""
One-time backfill: Postgres typed_edges + edges_v3 -> Neo4j.
Run once before switching reads to Neo4j (Phase 3).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'drift-memory'))
sys.path.insert(0, os.path.dirname(__file__))

import psycopg2.extras
from neo4j_adapter import get_graph

AGENTS = ['max', 'beth', 'susan', 'gerald', 'debater']


def get_conn():
    return psycopg2.connect(
        host=os.environ.get('DRIFT_DB_HOST', 'localhost'),
        port=int(os.environ.get('DRIFT_DB_PORT', 5433)),
        dbname=os.environ.get('DRIFT_DB_NAME', 'agent_memory'),
        user=os.environ.get('DRIFT_DB_USER', 'drift_admin'),
        password=os.environ.get('DRIFT_DB_PASSWORD', ''),
    )


def backfill_typed_edges(graph, agent):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT source_id, target_id, relationship, confidence, evidence, auto_extracted
            FROM {agent}.typed_edges
        """)
        rows = cur.fetchall()
    conn.close()

    count = 0
    for row in rows:
        try:
            graph.upsert_typed_edge(
                agent=agent,
                source_id=row['source_id'],
                target_id=row['target_id'],
                relationship=row['relationship'],
                confidence=float(row['confidence'] or 0.8),
                evidence=row['evidence'],
                auto_extracted=bool(row['auto_extracted']),
            )
            count += 1
        except Exception as e:
            print(f"  [skip] typed_edge {row['source_id']} -> {row['target_id']}: {e}")

    print(f"  {agent}: {count}/{len(rows)} typed_edges backfilled")
    return count


def backfill_cooccurrences(graph, agent):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT id1, id2, belief, platform_context, activity_context, topic_context
                FROM {agent}.edges_v3
            """)
            rows = cur.fetchall()
    except Exception as e:
        print(f"  {agent}: edges_v3 not found or empty ({e})")
        conn.close()
        return 0
    conn.close()

    count = 0
    for row in rows:
        try:
            graph.upsert_cooccurrence(
                agent=agent,
                id1=row['id1'],
                id2=row['id2'],
                belief=float(row['belief'] or 0.0),
                platform_context=row.get('platform_context') or {},
                activity_context=row.get('activity_context') or {},
                topic_context=row.get('topic_context') or {},
            )
            count += 1
        except Exception as e:
            print(f"  [skip] cooccur {row['id1']} - {row['id2']}: {e}")

    print(f"  {agent}: {count}/{len(rows)} cooccurrences backfilled")
    return count


def backfill_memory_nodes(graph, agent):
    """Ensure Memory nodes exist in Neo4j for all Postgres memories."""
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT id, type, importance, created
            FROM {agent}.memories
            WHERE type IN ('core', 'active')
        """)
        rows = cur.fetchall()
    conn.close()

    batch = [
        {
            'id': str(row['id']),
            'agent': agent,
            'type': row['type'],
            'importance': float(row['importance'] or 0.5),
        }
        for row in rows
    ]

    if batch:
        graph.write_batch("""
            UNWIND $batch AS m
            MERGE (n:Memory {id: m.id})
            ON CREATE SET n.agent = m.agent, n.type = m.type, n.importance = m.importance
            ON MATCH SET n.agent = m.agent, n.type = m.type
        """, batch)

    print(f"  {agent}: {len(batch)} memory nodes synced")
    return len(batch)


def main():
    graph = get_graph()
    graph.ensure_constraints()

    total_nodes = 0
    total_typed = 0
    total_cooccur = 0

    for agent in AGENTS:
        print(f"\n[{agent}]")
        total_nodes += backfill_memory_nodes(graph, agent)
        total_typed += backfill_typed_edges(graph, agent)
        total_cooccur += backfill_cooccurrences(graph, agent)

    print(f"\n=== Backfill Complete ===")
    print(f"Memory nodes: {total_nodes}")
    print(f"Typed edges:  {total_typed}")
    print(f"Cooccurrences: {total_cooccur}")

    # Verify
    print(f"\n=== Neo4j Counts ===")
    print(f"Memory nodes: {graph.count_nodes('Memory')}")
    print(f"TYPED_EDGE rels: {graph.count_relationships('TYPED_EDGE')}")
    print(f"COOCCURS rels: {graph.count_relationships('COOCCURS')}")


if __name__ == '__main__':
    main()
