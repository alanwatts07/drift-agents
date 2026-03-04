#!/usr/bin/env python3
"""
GraphRAG Retrieval — Phase 3: Community-aware memory retrieval.

Enhances the standard pgvector semantic search with graph context:
  1. pgvector finds seed memories (same as before)
  2. Neo4j expands seeds through graph edges → related memories
  3. Neo4j matches query against community summaries → cluster context
  4. Results are merged, deduplicated, and re-ranked

This module is called by memory_wrapper.py when DRIFT_USE_GRAPHRAG=1.

Usage (standalone test):
    python graph_retrieval.py <agent> "query text"
    python graph_retrieval.py max "what do I know about DeFi fraud?"

Architecture:
    pgvector seed → Neo4j graph expansion → community summary match → merged re-rank
"""

import os
import sys
from pathlib import Path

# Add graphrag dir and drift-memory to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "drift-memory"))

from neo4j_adapter import get_graph, close_driver


def graph_expand(agent: str, seed_ids: list, max_hops: int = 1, limit: int = 15) -> list:
    """
    Expand seed memory IDs through Neo4j graph edges.
    Returns list of related memory dicts not already in seeds.
    """
    if not seed_ids:
        return []

    graph = get_graph()

    # 1-hop expansion via any relationship
    results = graph.query("""
        UNWIND $seed_ids AS sid
        MATCH (seed:Memory {id: sid})-[r]-(neighbor:Memory {agent: $agent})
        WHERE neighbor.id <> sid AND NOT neighbor.id IN $seed_ids
        RETURN DISTINCT neighbor.id AS id, neighbor.content AS content,
               neighbor.importance AS importance, neighbor.type AS type,
               neighbor.tags AS tags, neighbor.created AS created,
               neighbor.community_id AS community_id,
               type(r) AS rel_type, r.confidence AS edge_weight
        ORDER BY neighbor.importance DESC
        LIMIT $limit
    """, {
        "seed_ids": seed_ids,
        "agent": agent,
        "limit": limit,
    })

    return results


def community_search(agent: str, query: str, limit: int = 5) -> list:
    """
    Search community summaries for query relevance.
    Returns communities whose title/summary/key_themes match the query.
    Uses Neo4j fulltext or contains matching.
    """
    graph = get_graph()

    # Text-based matching against community summaries
    # Split query into keywords for broader matching
    keywords = [w.lower() for w in query.split() if len(w) > 3]

    if not keywords:
        return []

    # Build WHERE clause — match any keyword in title, summary, or key_themes
    # Using toLower + CONTAINS for each keyword (OR logic)
    conditions = []
    params = {"agent": agent, "limit": limit}
    for i, kw in enumerate(keywords[:8]):  # Cap at 8 keywords
        key = f"kw{i}"
        params[key] = kw
        conditions.append(
            f"(toLower(c.title) CONTAINS ${key} OR "
            f"toLower(c.summary) CONTAINS ${key} OR "
            f"any(t IN c.key_themes WHERE toLower(t) CONTAINS ${key}))"
        )

    where = " OR ".join(conditions)

    results = graph.query(f"""
        MATCH (c:Community {{agent: $agent}})
        WHERE c.summary IS NOT NULL AND ({where})
        RETURN c.id AS community_id, c.title AS title, c.summary AS summary,
               c.key_themes AS key_themes, c.size AS size,
               c.avg_importance AS avg_importance
        ORDER BY c.size DESC
        LIMIT $limit
    """, params)

    return results


def get_community_members(community_id: str, limit: int = 10) -> list:
    """Pull top members of a community by importance."""
    graph = get_graph()
    return graph.query("""
        MATCH (m:Memory)-[:BELONGS_TO]->(c:Community {id: $id})
        RETURN m.id AS id, m.content AS content, m.importance AS importance,
               m.type AS type, m.tags AS tags, m.created AS created
        ORDER BY m.importance DESC
        LIMIT $limit
    """, {"id": community_id, "limit": limit})


def graphrag_search(agent: str, query: str, seed_ids: list = None,
                     max_graph_expand: int = 10, max_community: int = 5) -> dict:
    """
    Full GraphRAG retrieval pipeline.

    Args:
        agent: Agent name
        query: Search query text
        seed_ids: Memory IDs from pgvector search (optional — if None, skips expansion)
        max_graph_expand: Max memories from graph expansion
        max_community: Max communities to match

    Returns:
        {
            "graph_expanded": [...],       # Memories found via graph edges
            "community_matches": [...],    # Matching community summaries
            "community_members": [...],    # Top members from matching communities
        }
    """
    result = {
        "graph_expanded": [],
        "community_matches": [],
        "community_members": [],
    }

    # Step 1: Graph expansion from seeds
    if seed_ids:
        try:
            expanded = graph_expand(agent, seed_ids, limit=max_graph_expand)
            result["graph_expanded"] = expanded
        except Exception as e:
            print(f"[graphrag] Graph expansion failed: {e}", file=sys.stderr)

    # Step 2: Community summary search
    try:
        communities = community_search(agent, query, limit=max_community)
        result["community_matches"] = communities

        # Step 3: Pull top members from matching communities (exclude seeds)
        seen_ids = set(seed_ids or [])
        seen_ids.update(m["id"] for m in result["graph_expanded"])

        for comm in communities:
            members = get_community_members(comm["community_id"], limit=5)
            for mem in members:
                if mem["id"] not in seen_ids:
                    mem["from_community"] = comm["community_id"]
                    mem["community_title"] = comm["title"]
                    result["community_members"].append(mem)
                    seen_ids.add(mem["id"])

    except Exception as e:
        print(f"[graphrag] Community search failed: {e}", file=sys.stderr)

    return result


def format_graphrag_context(graphrag_result: dict, max_lines: int = 12) -> list:
    """
    Format GraphRAG results as context lines for the agent's prompt.
    Returns list of strings to append to the wake context.
    """
    lines = []
    items_shown = 0

    # Community matches — show summary-level context
    if graphrag_result["community_matches"]:
        lines.append("[Graph Communities]")
        for comm in graphrag_result["community_matches"][:3]:
            title = comm.get("title", "untitled")
            summary = (comm.get("summary") or "")[:150]
            lines.append(f"  Cluster: {title} ({comm['size']} memories)")
            if summary:
                lines.append(f"    {summary}")
            items_shown += 1

    # Graph-expanded memories
    if graphrag_result["graph_expanded"] and items_shown < max_lines:
        remaining = max_lines - items_shown
        for mem in graphrag_result["graph_expanded"][:remaining]:
            content = (mem.get("content") or "")[:150].replace('\n', ' ')
            rel = mem.get("rel_type", "RELATED")
            lines.append(f"[Graph/{rel}] {content}")
            items_shown += 1

    # Community member memories (not found by vector search)
    if graphrag_result["community_members"] and items_shown < max_lines:
        remaining = max_lines - items_shown
        for mem in graphrag_result["community_members"][:remaining]:
            content = (mem.get("content") or "")[:150].replace('\n', ' ')
            comm_title = mem.get("community_title", "")
            lines.append(f"[Cluster: {comm_title}] {content}")
            items_shown += 1

    return lines


# ============================================================
# Standalone test
# ============================================================

def main():
    """Standalone test: python graph_retrieval.py <agent> "query" """
    import argparse

    parser = argparse.ArgumentParser(description="GraphRAG retrieval test")
    parser.add_argument("agent", help="Agent name")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--seed-ids", nargs="*", help="Seed memory IDs (simulate pgvector hits)")
    args = parser.parse_args()

    # Setup agent env for db_adapter
    agent = args.agent
    schema = {'max': 'max', 'beth': 'beth', 'susan': 'susan',
              'debater': 'debater', 'gerald': 'gerald'}.get(agent, agent)
    os.environ['DRIFT_DB_SCHEMA'] = schema

    print(f"=== GraphRAG Search: {agent} ===")
    print(f"Query: {args.query}")

    # If no seed IDs provided, do a quick pgvector search to get some
    seed_ids = args.seed_ids or []
    if not seed_ids:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent / "drift-memory"))
            from db_adapter import get_db, db_to_file_metadata, reset_db
            reset_db()
            db = get_db()
            from semantic_search import get_embedding
            embedding = get_embedding(args.query)
            if embedding:
                rows = db.search_similar(embedding, limit=5)
                seed_ids = [db_to_file_metadata(r)[0]['id'] for r in rows]
                print(f"\npgvector seeds: {len(seed_ids)} memories")
                for r in rows:
                    meta, content = db_to_file_metadata(r)
                    preview = content[:120].replace('\n', ' ')
                    print(f"  [{meta['id'][:8]}] {preview}")
        except Exception as e:
            print(f"pgvector search failed (testing without seeds): {e}")

    # Run GraphRAG
    result = graphrag_search(agent, args.query, seed_ids=seed_ids)

    print(f"\n--- Graph Expansion ({len(result['graph_expanded'])} memories) ---")
    for mem in result["graph_expanded"]:
        content = (mem.get("content") or "")[:120].replace('\n', ' ')
        print(f"  [{mem['rel_type']}] {content}")

    print(f"\n--- Community Matches ({len(result['community_matches'])} communities) ---")
    for comm in result["community_matches"]:
        print(f"  {comm['community_id']}: {comm['title']} ({comm['size']} memories)")
        print(f"    {(comm.get('summary') or '')[:200]}")

    print(f"\n--- Community Members ({len(result['community_members'])} extra memories) ---")
    for mem in result["community_members"][:5]:
        content = (mem.get("content") or "")[:120].replace('\n', ' ')
        print(f"  [from {mem.get('community_title', '?')}] {content}")

    # Show formatted context
    print(f"\n--- Formatted Context ---")
    context_lines = format_graphrag_context(result)
    for line in context_lines:
        print(f"  {line}")

    close_driver()


if __name__ == "__main__":
    main()
