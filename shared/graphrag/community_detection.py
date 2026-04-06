#!/usr/bin/env python3
"""
Community Detection — Leiden algorithm on drift-agents' Neo4j graph.

Discovers natural knowledge communities in each agent's memory graph.
Uses the Leiden algorithm (Traag et al., 2019) via Python igraph + leidenalg,
pulling edges from Neo4j and writing community assignments back.

Usage:
    python community_detection.py run                # Detect communities for all agents
    python community_detection.py run max             # Single agent
    python community_detection.py status              # Show community stats
    python community_detection.py inspect <community_id>  # Show members of a community

Architecture:
    1. Pull memory nodes + edges from Neo4j
    2. Build igraph weighted graph
    3. Run Leiden algorithm (modularity optimization)
    4. Write community IDs back to Neo4j Memory nodes
    5. Create (:Community) nodes with metadata
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import igraph as ig
import leidenalg

# Add graphrag dir to path for neo4j_adapter
sys.path.insert(0, str(Path(__file__).parent))
from neo4j_adapter import get_graph, close_driver

AGENTS = ['max', 'beth', 'susan', 'debater', 'gerald', 'private_aye']


def pull_agent_graph(graph, agent: str) -> tuple:
    """
    Pull memory nodes and edges for an agent from Neo4j.
    Returns (nodes: list[dict], edges: list[dict]).
    """
    nodes = graph.query("""
        MATCH (m:Memory {agent: $agent})
        RETURN m.id AS id, m.type AS type, m.importance AS importance
    """, {"agent": agent})

    edges = graph.query("""
        MATCH (m1:Memory {agent: $agent})-[r:SIMILAR_TO|COLLABORATOR]->(m2:Memory {agent: $agent})
        RETURN m1.id AS source, m2.id AS target, r.confidence AS weight
    """, {"agent": agent})

    return nodes, edges


def build_igraph(nodes: list, edges: list) -> tuple:
    """
    Build an igraph Graph from Neo4j nodes and edges.
    Returns (graph, id_to_idx mapping, idx_to_id mapping).
    """
    id_to_idx = {}
    idx_to_id = {}
    for i, node in enumerate(nodes):
        id_to_idx[node["id"]] = i
        idx_to_id[i] = node["id"]

    g = ig.Graph(n=len(nodes), directed=False)

    # Add node attributes
    g.vs["memory_id"] = [n["id"] for n in nodes]
    g.vs["type"] = [n.get("type") or "active" for n in nodes]
    g.vs["importance"] = [n.get("importance") or 0.5 for n in nodes]

    # Add edges (skip if either node missing)
    edge_list = []
    weights = []
    for e in edges:
        src = id_to_idx.get(e["source"])
        tgt = id_to_idx.get(e["target"])
        if src is not None and tgt is not None and src != tgt:
            edge_list.append((src, tgt))
            weights.append(e.get("weight") or 0.8)

    if edge_list:
        g.add_edges(edge_list)
        g.es["weight"] = weights

    return g, id_to_idx, idx_to_id


def run_leiden(g: ig.Graph, resolution: float = 1.0) -> list:
    """
    Run Leiden algorithm on the graph.
    Returns partition (list of community IDs, one per vertex).
    """
    if g.ecount() == 0:
        # No edges — each node is its own community
        return list(range(g.vcount()))

    partition = leidenalg.find_partition(
        g,
        leidenalg.ModularityVertexPartition,
        weights="weight" if "weight" in g.es.attributes() else None,
        n_iterations=10,
        seed=42,
    )
    return partition.membership


def compute_community_metadata(g: ig.Graph, membership: list, agent: str) -> list:
    """
    Compute metadata for each community.
    Returns list of community dicts with stats.
    """
    communities = defaultdict(list)
    for idx, comm_id in enumerate(membership):
        communities[comm_id].append(idx)

    results = []
    for comm_id, member_indices in sorted(communities.items()):
        members = member_indices
        member_ids = [g.vs[i]["memory_id"] for i in members]

        # Aggregate importance
        importances = [g.vs[i]["importance"] or 0.5 for i in members]
        avg_importance = sum(importances) / len(importances) if importances else 0.5

        # Memory types breakdown
        types = Counter(g.vs[i]["type"] for i in members)

        results.append({
            "community_id": f"{agent}_c{comm_id}",
            "agent": agent,
            "local_id": comm_id,
            "size": len(members),
            "member_ids": member_ids,
            "avg_importance": round(avg_importance, 3),
            "type_breakdown": dict(types),
        })

    return results


def write_communities_to_neo4j(graph, agent: str, membership: list,
                                communities: list, ig_graph: ig.Graph):
    """
    Write community assignments back to Neo4j.
    - Clear old communities for this agent
    - Set community_id property on Memory nodes
    - Create (:Community) nodes with metadata
    """
    # Clear old communities for this agent
    graph.write("""
        MATCH (c:Community {agent: $agent})
        DETACH DELETE c
    """, {"agent": agent})
    graph.write("""
        MATCH (m:Memory {agent: $agent})
        REMOVE m.community_id
    """, {"agent": agent})

    # Update Memory nodes with community assignments
    updates = []
    for idx, comm_id in enumerate(membership):
        updates.append({
            "memory_id": ig_graph.vs[idx]["memory_id"],
            "community_id": f"{agent}_c{comm_id}",
        })

    if updates:
        graph.write_batch("""
            UNWIND $batch AS u
            MATCH (m:Memory {id: u.memory_id})
            SET m.community_id = u.community_id
        """, updates)

    # Create/update Community nodes
    for comm in communities:
        graph.write("""
            MERGE (c:Community {id: $id})
            SET c.agent = $agent,
                c.size = $size,
                c.avg_importance = $avg_importance,
                c.type_breakdown = $type_breakdown,
                c.updated_at = datetime()
        """, {
            "id": comm["community_id"],
            "agent": comm["agent"],
            "size": comm["size"],
            "avg_importance": comm["avg_importance"],
            "type_breakdown": json.dumps(comm["type_breakdown"]),
        })

        # Link memories to their community
        graph.write("""
            MATCH (c:Community {id: $comm_id})
            UNWIND $member_ids AS mid
            MATCH (m:Memory {id: mid})
            MERGE (m)-[:BELONGS_TO]->(c)
        """, {
            "comm_id": comm["community_id"],
            "member_ids": comm["member_ids"],
        })

    # Link agent to communities
    graph.write("""
        MATCH (a:Agent {name: $agent})
        MATCH (c:Community {agent: $agent})
        MERGE (a)-[:HAS_COMMUNITY]->(c)
    """, {"agent": agent})


def detect_communities(agent: str, resolution: float = 1.0):
    """Full pipeline: pull graph → Leiden → write back."""
    print(f"\n=== Community detection: {agent} ===")
    start = time.time()
    graph = get_graph()

    # Pull data
    nodes, edges = pull_agent_graph(graph, agent)
    print(f"  Nodes: {len(nodes)}, Edges: {len(edges)}")

    if not nodes:
        print(f"  No memories for {agent}, skipping")
        return []

    # Build igraph
    ig_graph, id_to_idx, idx_to_id = build_igraph(nodes, edges)
    print(f"  igraph: {ig_graph.vcount()} vertices, {ig_graph.ecount()} edges")

    # Run Leiden
    membership = run_leiden(ig_graph, resolution=resolution)
    n_communities = len(set(membership))
    print(f"  Leiden found {n_communities} communities")

    # Compute metadata
    communities = compute_community_metadata(ig_graph, membership, agent)

    # Sort by size descending
    communities.sort(key=lambda c: c["size"], reverse=True)

    # Print summary
    for comm in communities[:10]:
        types_str = ", ".join(f"{k}:{v}" for k, v in comm["type_breakdown"].items())
        print(f"    {comm['community_id']}: {comm['size']} memories "
              f"(imp={comm['avg_importance']:.2f}) [{types_str}]")
    if len(communities) > 10:
        print(f"    ... and {len(communities) - 10} more communities")

    # Write to Neo4j
    write_communities_to_neo4j(graph, agent, membership, communities, ig_graph)
    elapsed = time.time() - start
    print(f"  Done in {elapsed:.1f}s — {n_communities} communities written to Neo4j")

    return communities


def detect_all(resolution: float = 1.0):
    """Run community detection for all agents."""
    all_communities = {}
    for agent in AGENTS:
        try:
            communities = detect_communities(agent, resolution=resolution)
            all_communities[agent] = communities
        except Exception as e:
            print(f"  [{agent}] Error: {e}")

    # Summary
    print(f"\n=== Summary ===")
    total_comms = 0
    for agent, comms in all_communities.items():
        n = len(comms)
        total_comms += n
        sizes = [c["size"] for c in comms] if comms else [0]
        print(f"  {agent}: {n} communities "
              f"(largest={max(sizes)}, smallest={min(sizes)}, median={sorted(sizes)[len(sizes)//2]})")
    print(f"  Total: {total_comms} communities across {len(all_communities)} agents")


def show_status():
    """Show community stats from Neo4j."""
    graph = get_graph()

    result = graph.query("""
        MATCH (c:Community)
        RETURN c.agent AS agent, count(c) AS communities, sum(c.size) AS total_memories,
               avg(c.size) AS avg_size, max(c.size) AS max_size
        ORDER BY communities DESC
    """)

    if not result:
        print("No communities detected yet. Run: python community_detection.py run")
        return

    print("=== Community Status ===")
    for row in result:
        print(f"  {row['agent']}: {row['communities']} communities, "
              f"avg size={row['avg_size']:.1f}, largest={row['max_size']}")

    # Top communities by size
    top = graph.query("""
        MATCH (c:Community)
        RETURN c.id AS id, c.agent AS agent, c.size AS size,
               c.top_tags AS tags, c.avg_importance AS importance
        ORDER BY c.size DESC
        LIMIT 15
    """)
    if top:
        print("\n  Top 15 communities:")
        for row in top:
            tags = ", ".join(row["tags"][:3]) if row["tags"] else "—"
            print(f"    {row['id']}: {row['size']} memories "
                  f"(imp={row['importance']:.2f}) [{tags}]")


def inspect_community(community_id: str):
    """Show details of a specific community."""
    graph = get_graph()

    # Community metadata
    result = graph.query("""
        MATCH (c:Community {id: $id})
        RETURN c
    """, {"id": community_id})

    if not result:
        print(f"Community not found: {community_id}")
        return

    comm = result[0]["c"]
    print(f"=== Community: {community_id} ===")
    print(f"  Agent: {comm.get('agent')}")
    print(f"  Size: {comm.get('size')}")
    print(f"  Avg importance: {comm.get('avg_importance')}")
    print(f"  Top tags: {comm.get('top_tags')}")
    print(f"  Type breakdown: {comm.get('type_breakdown')}")

    # Member memories
    members = graph.query("""
        MATCH (m:Memory)-[:BELONGS_TO]->(c:Community {id: $id})
        RETURN m.id AS id, m.content AS content, m.importance AS importance,
               m.type AS type, m.tags AS tags
        ORDER BY m.importance DESC
        LIMIT 20
    """, {"id": community_id})

    if members:
        print(f"\n  Top memories (by importance):")
        for m in members:
            content = (m["content"] or "")[:120]
            print(f"    [{m['type']}] (imp={m['importance']:.2f}) {content}")


def main():
    parser = argparse.ArgumentParser(description="Leiden community detection for drift-agents")
    parser.add_argument("command", choices=["run", "status", "inspect"],
                        help="Command to execute")
    parser.add_argument("agent", nargs="?", help="Agent name or community ID")
    parser.add_argument("--resolution", type=float, default=1.0,
                        help="Leiden resolution parameter (higher = more communities)")
    args = parser.parse_args()

    if args.command == "run":
        if args.agent and args.agent in AGENTS:
            detect_communities(args.agent, resolution=args.resolution)
        else:
            detect_all(resolution=args.resolution)
    elif args.command == "status":
        show_status()
    elif args.command == "inspect":
        if not args.agent:
            print("Usage: community_detection.py inspect <community_id>")
            return
        inspect_community(args.agent)

    close_driver()


if __name__ == "__main__":
    main()
