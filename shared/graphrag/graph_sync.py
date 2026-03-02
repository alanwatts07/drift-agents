#!/usr/bin/env python3
"""
Graph Sync — PostgreSQL → Neo4j incremental sync for drift-agents.

Mirrors memory data from PostgreSQL (source of truth) into Neo4j (graph engine).
Designed to run after each sleep phase or as a standalone batch sync.

Usage:
    python graph_sync.py full <agent>          # Full sync of agent's data
    python graph_sync.py full --all            # Full sync of all agents
    python graph_sync.py incremental <agent>   # Sync since last checkpoint
    python graph_sync.py status                # Show sync stats

Architecture:
    PostgreSQL memories  →  Neo4j (:Memory) nodes
    PostgreSQL edges_v3  →  Neo4j [:COOCCURS] relationships
    PostgreSQL typed_edges → Neo4j [:CAUSES|:ENABLES|...] relationships
    PostgreSQL shared.memories → Neo4j (:SharedMemory) nodes
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add drift-memory to path
DRIFT_MEMORY_DIR = Path(__file__).parent
if str(DRIFT_MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(DRIFT_MEMORY_DIR))

import psycopg2
import psycopg2.extras
from neo4j_adapter import get_graph, close_driver

AGENTS = ['max', 'beth', 'susan', 'debater', 'gerald']

# Typed edge relationship mapping (PostgreSQL relationship → Neo4j type)
REL_TYPE_MAP = {
    'causes': 'CAUSES',
    'enables': 'ENABLES',
    'contradicts': 'CONTRADICTS',
    'supersedes': 'SUPERSEDES',
    'part_of': 'PART_OF',
    'instance_of': 'INSTANCE_OF',
    'similar_to': 'SIMILAR_TO',
    'depends_on': 'DEPENDS_ON',
    'implements': 'IMPLEMENTS',
    'learned_from': 'LEARNED_FROM',
    'collaborator': 'COLLABORATOR',
    'temporal_before': 'TEMPORAL_BEFORE',
    'temporal_after': 'TEMPORAL_AFTER',
    'references': 'REFERENCES',
    'resolves': 'RESOLVES',
    'supports': 'SUPPORTS',
    'counterfactual_of': 'COUNTERFACTUAL_OF',
}


def get_pg_conn():
    """Get a PostgreSQL connection."""
    return psycopg2.connect(
        host=os.environ.get('DRIFT_DB_HOST', 'localhost'),
        port=int(os.environ.get('DRIFT_DB_PORT', '5433')),
        dbname=os.environ.get('DRIFT_DB_NAME', 'agent_memory'),
        user=os.environ.get('DRIFT_DB_USER', 'drift_admin'),
        password=os.environ.get('DRIFT_DB_PASSWORD', 'drift_agents_local_dev'),
    )


def sync_agent_memories(graph, pg_conn, agent: str):
    """Sync all memories for an agent from PostgreSQL to Neo4j."""
    cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Ensure agent node exists
    graph.write(
        "MERGE (a:Agent {name: $name})",
        {"name": agent}
    )

    # Fetch all memories
    cur.execute(f"""
        SELECT id, type, content, created, last_recalled, recall_count,
               emotional_weight, importance, freshness, memory_tier,
               q_value, tags, entities
        FROM {agent}.memories
    """)
    memories = cur.fetchall()

    if not memories:
        print(f"  [{agent}] No memories to sync")
        return 0

    # Batch upsert memories as nodes
    batch = []
    for m in memories:
        batch.append({
            "id": m["id"],
            "agent": agent,
            "type": m["type"],
            "content": m["content"][:500] if m["content"] else "",
            "content_full": m["content"] or "",
            "created": m["created"].isoformat() if m["created"] else None,
            "last_recalled": m["last_recalled"].isoformat() if m["last_recalled"] else None,
            "recall_count": m["recall_count"] or 0,
            "emotional_weight": m["emotional_weight"] or 0.5,
            "importance": m["importance"] or 0.5,
            "freshness": m["freshness"] or 1.0,
            "memory_tier": m["memory_tier"] or "episodic",
            "q_value": m["q_value"] or 0.5,
            "tags": m["tags"] or [],
        })

    graph.write_batch("""
        UNWIND $batch AS mem
        MERGE (m:Memory {id: mem.id})
        SET m.agent = mem.agent,
            m.type = mem.type,
            m.content = mem.content,
            m.content_full = mem.content_full,
            m.created = mem.created,
            m.last_recalled = mem.last_recalled,
            m.recall_count = mem.recall_count,
            m.emotional_weight = mem.emotional_weight,
            m.importance = mem.importance,
            m.freshness = mem.freshness,
            m.memory_tier = mem.memory_tier,
            m.q_value = mem.q_value,
            m.tags = mem.tags
        WITH m, mem
        MATCH (a:Agent {name: mem.agent})
        MERGE (a)-[:OWNS]->(m)
    """, batch)

    print(f"  [{agent}] Synced {len(memories)} memories")
    cur.close()
    return len(memories)


def sync_agent_cooccurrences(graph, pg_conn, agent: str):
    """Sync edges_v3 (co-occurrence edges) to Neo4j."""
    cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(f"""
        SELECT id1, id2, belief, first_formed, last_updated
        FROM {agent}.edges_v3
    """)
    edges = cur.fetchall()

    if not edges:
        print(f"  [{agent}] No co-occurrence edges to sync")
        return 0

    batch = []
    for e in edges:
        batch.append({
            "id1": e["id1"],
            "id2": e["id2"],
            "belief": e["belief"] or 0.0,
            "first_formed": e["first_formed"].isoformat() if e["first_formed"] else None,
            "last_updated": e["last_updated"].isoformat() if e["last_updated"] else None,
        })

    graph.write_batch("""
        UNWIND $batch AS edge
        MATCH (m1:Memory {id: edge.id1})
        MATCH (m2:Memory {id: edge.id2})
        MERGE (m1)-[r:COOCCURS]->(m2)
        SET r.belief = edge.belief,
            r.first_formed = edge.first_formed,
            r.last_updated = edge.last_updated
    """, batch)

    print(f"  [{agent}] Synced {len(edges)} co-occurrence edges")
    cur.close()
    return len(edges)


def sync_agent_typed_edges(graph, pg_conn, agent: str):
    """Sync typed_edges (semantic relationships) to Neo4j."""
    cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(f"""
        SELECT source_id, target_id, relationship, confidence, evidence, auto_extracted
        FROM {agent}.typed_edges
    """)
    edges = cur.fetchall()

    if not edges:
        print(f"  [{agent}] No typed edges to sync")
        return 0

    # Group by relationship type for efficient Cypher
    count = 0
    by_type = {}
    for e in edges:
        rel = e["relationship"]
        neo4j_type = REL_TYPE_MAP.get(rel, rel.upper())
        by_type.setdefault(neo4j_type, []).append({
            "source": e["source_id"],
            "target": e["target_id"],
            "confidence": e["confidence"] or 0.8,
            "evidence": (e["evidence"] or "")[:200],
            "auto_extracted": e["auto_extracted"] or False,
        })

    for rel_type, batch in by_type.items():
        # Neo4j doesn't allow parameterized relationship types, so we use string formatting
        # (rel_type is from our controlled mapping, not user input)
        graph.write_batch(f"""
            UNWIND $batch AS edge
            MATCH (m1:Memory {{id: edge.source}})
            MATCH (m2:Memory {{id: edge.target}})
            MERGE (m1)-[r:{rel_type}]->(m2)
            SET r.confidence = edge.confidence,
                r.evidence = edge.evidence,
                r.auto_extracted = edge.auto_extracted
        """, batch)
        count += len(batch)

    print(f"  [{agent}] Synced {count} typed edges ({len(by_type)} relationship types)")
    cur.close()
    return count


def sync_shared_memories(graph, pg_conn):
    """Sync shared cross-agent memories."""
    cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT id, content, created, created_by, tags, emotional_weight, importance
        FROM shared.memories
    """)
    memories = cur.fetchall()

    if not memories:
        print("  [shared] No shared memories to sync")
        return 0

    batch = []
    for m in memories:
        batch.append({
            "id": m["id"],
            "content": m["content"][:500] if m["content"] else "",
            "content_full": m["content"] or "",
            "created": m["created"].isoformat() if m["created"] else None,
            "created_by": m["created_by"],
            "tags": m["tags"] or [],
            "emotional_weight": m["emotional_weight"] or 0.5,
            "importance": m["importance"] or 0.5,
        })

    graph.write_batch("""
        UNWIND $batch AS mem
        MERGE (m:SharedMemory {id: mem.id})
        SET m.content = mem.content,
            m.content_full = mem.content_full,
            m.created = mem.created,
            m.created_by = mem.created_by,
            m.tags = mem.tags,
            m.emotional_weight = mem.emotional_weight,
            m.importance = mem.importance
        WITH m, mem
        MATCH (a:Agent {name: mem.created_by})
        MERGE (a)-[:SHARED]->(m)
    """, batch)

    print(f"  [shared] Synced {len(memories)} shared memories")
    cur.close()
    return len(memories)


def sync_agent_lessons(graph, pg_conn, agent: str):
    """Sync lessons to Neo4j."""
    cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(f"""
        SELECT id, category, lesson, confidence, applied_count
        FROM {agent}.lessons
    """)
    lessons = cur.fetchall()

    if not lessons:
        return 0

    batch = []
    for l in lessons:
        batch.append({
            "id": l["id"],
            "agent": agent,
            "category": l["category"] or "general",
            "lesson": l["lesson"] or "",
            "confidence": l["confidence"] or 0.7,
            "applied_count": l["applied_count"] or 0,
        })

    graph.write_batch("""
        UNWIND $batch AS les
        MERGE (l:Lesson {id: les.id})
        SET l.agent = les.agent,
            l.category = les.category,
            l.lesson = les.lesson,
            l.confidence = les.confidence,
            l.applied_count = les.applied_count
        WITH l, les
        MATCH (a:Agent {name: les.agent})
        MERGE (a)-[:LEARNED]->(l)
    """, batch)

    print(f"  [{agent}] Synced {len(lessons)} lessons")
    cur.close()
    return len(lessons)


def full_sync(agent: str):
    """Full sync of an agent's data from PostgreSQL to Neo4j."""
    print(f"Full sync: {agent}")
    start = time.time()

    graph = get_graph()
    graph.ensure_constraints()
    pg_conn = get_pg_conn()

    os.environ['DRIFT_DB_SCHEMA'] = agent

    totals = {
        "memories": sync_agent_memories(graph, pg_conn, agent),
        "cooccurrences": sync_agent_cooccurrences(graph, pg_conn, agent),
        "typed_edges": sync_agent_typed_edges(graph, pg_conn, agent),
        "lessons": sync_agent_lessons(graph, pg_conn, agent),
    }

    pg_conn.close()
    elapsed = time.time() - start
    print(f"  [{agent}] Done in {elapsed:.1f}s — {sum(totals.values())} total items synced")
    return totals


def full_sync_all():
    """Full sync of all agents + shared data."""
    print("=== Full sync: all agents ===")
    start = time.time()

    graph = get_graph()
    graph.ensure_constraints()
    pg_conn = get_pg_conn()

    for agent in AGENTS:
        try:
            os.environ['DRIFT_DB_SCHEMA'] = agent
            sync_agent_memories(graph, pg_conn, agent)
            sync_agent_cooccurrences(graph, pg_conn, agent)
            sync_agent_typed_edges(graph, pg_conn, agent)
            sync_agent_lessons(graph, pg_conn, agent)
        except Exception as e:
            print(f"  [{agent}] Error: {e}")
            pg_conn.rollback()

    try:
        sync_shared_memories(graph, pg_conn)
    except Exception as e:
        print(f"  [shared] Error: {e}")
        pg_conn.rollback()

    pg_conn.close()
    elapsed = time.time() - start

    # Print stats
    print(f"\n=== Neo4j Stats ===")
    print(f"  Memory nodes: {graph.count_nodes('Memory')}")
    print(f"  SharedMemory nodes: {graph.count_nodes('SharedMemory')}")
    print(f"  Lesson nodes: {graph.count_nodes('Lesson')}")
    print(f"  Agent nodes: {graph.count_nodes('Agent')}")
    print(f"  COOCCURS edges: {graph.count_relationships('COOCCURS')}")
    total_typed = sum(
        graph.count_relationships(rt) for rt in REL_TYPE_MAP.values()
    )
    print(f"  Typed edges: {total_typed}")
    print(f"  Total sync time: {elapsed:.1f}s")


def show_status():
    """Show current Neo4j graph stats."""
    graph = get_graph()
    print("=== Neo4j Graph Status ===")
    print(f"  Memory nodes: {graph.count_nodes('Memory')}")
    print(f"  SharedMemory nodes: {graph.count_nodes('SharedMemory')}")
    print(f"  Lesson nodes: {graph.count_nodes('Lesson')}")
    print(f"  Agent nodes: {graph.count_nodes('Agent')}")
    print(f"  COOCCURS edges: {graph.count_relationships('COOCCURS')}")

    # Per-agent counts
    result = graph.query("""
        MATCH (a:Agent)-[:OWNS]->(m:Memory)
        RETURN a.name AS agent, count(m) AS memories
        ORDER BY memories DESC
    """)
    if result:
        print("\n  Per-agent memories:")
        for row in result:
            print(f"    {row['agent']}: {row['memories']}")

    # Relationship type counts
    result = graph.query("""
        MATCH ()-[r]->()
        RETURN type(r) AS rel_type, count(r) AS count
        ORDER BY count DESC
        LIMIT 15
    """)
    if result:
        print("\n  Relationship types:")
        for row in result:
            print(f"    {row['rel_type']}: {row['count']}")


def main():
    parser = argparse.ArgumentParser(description="PostgreSQL → Neo4j graph sync")
    parser.add_argument("command", choices=["full", "incremental", "status"],
                        help="Sync mode")
    parser.add_argument("agent", nargs="?", help="Agent name (or --all)")
    parser.add_argument("--all", action="store_true", help="Sync all agents")
    args = parser.parse_args()

    if args.command == "status":
        show_status()
    elif args.command == "full":
        if args.all or not args.agent:
            full_sync_all()
        else:
            full_sync(args.agent)
    elif args.command == "incremental":
        # TODO: Phase 1 — track last_synced timestamp, only sync new/changed records
        print("Incremental sync not yet implemented — running full sync")
        if args.all or not args.agent:
            full_sync_all()
        else:
            full_sync(args.agent)

    close_driver()


if __name__ == "__main__":
    main()
