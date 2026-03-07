"""
Neo4j adapter for drift-agents GraphRAG pipeline.

Manages connection pool and provides Cypher helpers for the graph layer.
Neo4j runs alongside PostgreSQL — reads from graph, writes still go to PG first.

Usage:
    from neo4j_adapter import get_graph, close_graph
    g = get_graph()
    result = g.query("MATCH (n:Memory) RETURN count(n) AS count")
"""

import os
from neo4j import GraphDatabase

_driver = None


def get_driver():
    """Get or create the Neo4j driver (singleton)."""
    global _driver
    if _driver is None:
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        password = os.environ.get("NEO4J_PASSWORD", "drift_graph_local")
        _driver = GraphDatabase.driver(uri, auth=(user, password))
    return _driver


def close_driver():
    """Close the Neo4j driver."""
    global _driver
    if _driver:
        _driver.close()
        _driver = None


def reset_driver():
    """Reset the driver (e.g., after config change)."""
    close_driver()


class GraphDB:
    """Convenience wrapper around the Neo4j driver."""

    def __init__(self):
        self.driver = get_driver()

    def query(self, cypher: str, params: dict = None, database: str = "neo4j") -> list:
        """Execute a Cypher query and return list of record dicts."""
        with self.driver.session(database=database) as session:
            result = session.run(cypher, params or {})
            return [record.data() for record in result]

    def write(self, cypher: str, params: dict = None, database: str = "neo4j"):
        """Execute a write Cypher query within a transaction."""
        with self.driver.session(database=database) as session:
            session.execute_write(lambda tx: tx.run(cypher, params or {}))

    def write_batch(self, cypher: str, batch: list, database: str = "neo4j"):
        """Execute a Cypher query for each item in batch using UNWIND."""
        with self.driver.session(database=database) as session:
            session.execute_write(
                lambda tx: tx.run(cypher, {"batch": batch})
            )

    def count_nodes(self, label: str = "Memory") -> int:
        """Count nodes with a given label."""
        result = self.query(f"MATCH (n:{label}) RETURN count(n) AS count")
        return result[0]["count"] if result else 0

    def count_relationships(self, rel_type: str = None) -> int:
        """Count relationships, optionally filtered by type."""
        if rel_type:
            result = self.query(f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS count")
        else:
            result = self.query("MATCH ()-[r]->() RETURN count(r) AS count")
        return result[0]["count"] if result else 0

    def ensure_constraints(self):
        """Create uniqueness constraints and indexes."""
        constraints = [
            "CREATE CONSTRAINT memory_id IF NOT EXISTS FOR (m:Memory) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT shared_memory_id IF NOT EXISTS FOR (m:SharedMemory) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT agent_name IF NOT EXISTS FOR (a:Agent) REQUIRE a.name IS UNIQUE",
            "CREATE INDEX memory_agent IF NOT EXISTS FOR (m:Memory) ON (m.agent)",
            "CREATE INDEX memory_type IF NOT EXISTS FOR (m:Memory) ON (m.type)",
            "CREATE INDEX memory_created IF NOT EXISTS FOR (m:Memory) ON (m.created)",
            "CREATE INDEX memory_importance IF NOT EXISTS FOR (m:Memory) ON (m.importance)",
            "CREATE INDEX cooccurs_agent IF NOT EXISTS FOR ()-[r:COOCCURS]-() ON (r.agent)",
            "CREATE INDEX typed_edge_agent IF NOT EXISTS FOR ()-[r:TYPED_EDGE]-() ON (r.agent)",
            "CREATE INDEX typed_edge_rel IF NOT EXISTS FOR ()-[r:TYPED_EDGE]-() ON (r.relationship)",
        ]
        for cypher in constraints:
            try:
                self.write(cypher)
            except Exception:
                pass  # Constraint may already exist

    # ------------------------------------------------------------------
    # Phase 1: Typed edges
    # ------------------------------------------------------------------

    def upsert_typed_edge(
        self,
        agent: str,
        source_id: str,
        target_id: str,
        relationship: str,
        confidence: float = 0.8,
        evidence: str = None,
        auto_extracted: bool = False,
    ):
        """Upsert a TYPED_EDGE relationship between two Memory nodes.

        MERGEs on (source_id, target_id, relationship, agent).
        On create sets all properties; on match keeps the higher confidence
        and coalesces evidence.
        """
        cypher = (
            "MATCH (s:Memory {id: $source_id}), (t:Memory {id: $target_id}) "
            "MERGE (s)-[r:TYPED_EDGE {relationship: $rel, agent: $agent}]->(t) "
            "ON CREATE SET r.confidence=$conf, r.evidence=$evidence, "
            "r.auto_extracted=$auto_extracted, r.created=datetime() "
            "ON MATCH SET "
            "r.confidence=CASE WHEN $conf > r.confidence THEN $conf ELSE r.confidence END, "
            "r.evidence=COALESCE($evidence, r.evidence)"
        )
        self.write(
            cypher,
            {
                "source_id": source_id,
                "target_id": target_id,
                "rel": relationship,
                "agent": agent,
                "conf": confidence,
                "evidence": evidence,
                "auto_extracted": auto_extracted,
            },
        )

    def get_typed_edges_from(self, source_id: str, relationship: str = None) -> list:
        """Return all outgoing TYPED_EDGE relationships from a Memory node.

        Optionally filter by relationship type.
        """
        if relationship:
            cypher = (
                "MATCH (s:Memory {id: $source_id})-[r:TYPED_EDGE]->(t:Memory) "
                "WHERE r.relationship = $relationship "
                "RETURN s.id AS source_id, t.id AS target_id, "
                "r.relationship AS relationship, r.confidence AS confidence, "
                "r.evidence AS evidence, r.auto_extracted AS auto_extracted"
            )
            params = {"source_id": source_id, "relationship": relationship}
        else:
            cypher = (
                "MATCH (s:Memory {id: $source_id})-[r:TYPED_EDGE]->(t:Memory) "
                "RETURN s.id AS source_id, t.id AS target_id, "
                "r.relationship AS relationship, r.confidence AS confidence, "
                "r.evidence AS evidence, r.auto_extracted AS auto_extracted"
            )
            params = {"source_id": source_id}
        return self.query(cypher, params)

    def get_typed_edges_to(self, target_id: str, relationship: str = None) -> list:
        """Return all incoming TYPED_EDGE relationships to a Memory node.

        Optionally filter by relationship type.
        """
        if relationship:
            cypher = (
                "MATCH (s:Memory)-[r:TYPED_EDGE]->(t:Memory {id: $target_id}) "
                "WHERE r.relationship = $relationship "
                "RETURN s.id AS source_id, t.id AS target_id, "
                "r.relationship AS relationship, r.confidence AS confidence, "
                "r.evidence AS evidence, r.auto_extracted AS auto_extracted"
            )
            params = {"target_id": target_id, "relationship": relationship}
        else:
            cypher = (
                "MATCH (s:Memory)-[r:TYPED_EDGE]->(t:Memory {id: $target_id}) "
                "RETURN s.id AS source_id, t.id AS target_id, "
                "r.relationship AS relationship, r.confidence AS confidence, "
                "r.evidence AS evidence, r.auto_extracted AS auto_extracted"
            )
            params = {"target_id": target_id}
        return self.query(cypher, params)

    def get_all_typed_edges(self, memory_id: str) -> list:
        """Return all TYPED_EDGE relationships in both directions for a Memory node.

        Each record includes a 'direction' field ('outgoing' or 'incoming').
        """
        cypher = (
            "MATCH (s:Memory {id: $memory_id})-[r:TYPED_EDGE]->(t:Memory) "
            "RETURN s.id AS source_id, t.id AS target_id, "
            "r.relationship AS relationship, r.confidence AS confidence, "
            "r.evidence AS evidence, r.auto_extracted AS auto_extracted, "
            "'outgoing' AS direction "
            "UNION "
            "MATCH (s:Memory)-[r:TYPED_EDGE]->(t:Memory {id: $memory_id}) "
            "RETURN s.id AS source_id, t.id AS target_id, "
            "r.relationship AS relationship, r.confidence AS confidence, "
            "r.evidence AS evidence, r.auto_extracted AS auto_extracted, "
            "'incoming' AS direction"
        )
        return self.query(cypher, {"memory_id": memory_id})

    def delete_typed_edge(
        self,
        agent: str,
        source_id: str,
        target_id: str,
        relationship: str,
    ):
        """Delete a specific TYPED_EDGE relationship between two Memory nodes."""
        cypher = (
            "MATCH (s:Memory {id: $source_id})-[r:TYPED_EDGE {relationship: $rel, agent: $agent}]"
            "->(t:Memory {id: $target_id}) "
            "DELETE r"
        )
        self.write(
            cypher,
            {
                "source_id": source_id,
                "target_id": target_id,
                "rel": relationship,
                "agent": agent,
            },
        )

    def traverse(
        self,
        start_id: str,
        relationship: str = None,
        hops: int = 2,
        direction: str = "outgoing",
        min_confidence: float = 0.3,
    ) -> list:
        """Traverse TYPED_EDGE relationships from a starting Memory node.

        Supports 'outgoing', 'incoming', and 'both' directions.
        Filters by minimum confidence on every relationship in the path.
        Optionally filters by relationship type.
        Returns deduplicated edge dicts with a 'depth' field.
        """
        # hops must be interpolated directly — Neo4j doesn't support params in *1..N
        h = int(hops)
        if direction == "outgoing":
            path_pattern = f"(start:Memory {{id: $start_id}})-[r:TYPED_EDGE*1..{h}]->(end:Memory)"
        elif direction == "incoming":
            path_pattern = f"(start:Memory {{id: $start_id}})<-[r:TYPED_EDGE*1..{h}]-(end:Memory)"
        else:
            path_pattern = f"(start:Memory {{id: $start_id}})-[r:TYPED_EDGE*1..{h}]-(end:Memory)"

        confidence_filter = "ALL(rel IN relationships(path) WHERE rel.confidence >= $min_confidence)"

        if relationship:
            rel_filter = (
                " AND ALL(rel IN relationships(path) WHERE rel.relationship = $rel_type)"
            )
        else:
            rel_filter = ""

        cypher = (
            f"MATCH path = {path_pattern} "
            f"WHERE {confidence_filter}{rel_filter} "
            "UNWIND relationships(path) AS rel "
            "RETURN startNode(rel).id AS source_id, endNode(rel).id AS target_id, "
            "rel.relationship AS relationship, rel.confidence AS confidence, "
            "rel.evidence AS evidence, rel.auto_extracted AS auto_extracted, "
            "length(path) AS depth"
        )
        params = {
            "start_id": start_id,
            "hops": hops,
            "min_confidence": min_confidence,
        }
        if relationship:
            params["rel_type"] = relationship

        rows = self.query(cypher, params)

        # Deduplicate by (source_id, target_id, relationship)
        seen = set()
        results = []
        for row in rows:
            key = (row["source_id"], row["target_id"], row["relationship"])
            if key not in seen:
                seen.add(key)
                results.append(row)
        return results

    def find_path(self, id1: str, id2: str, max_hops: int = 5) -> "dict | None":
        """Find the shortest path between two Memory nodes via TYPED_EDGE.

        Returns a dict with 'depth' (number of relationships) and 'edges'
        (list of edge dicts), or None if no path exists.
        """
        h = int(max_hops)
        cypher = (
            f"MATCH path = shortestPath("
            f"(a:Memory {{id: $id1}})-[r:TYPED_EDGE*1..{h}]-(b:Memory {{id: $id2}})"
            f") "
            "RETURN [rel IN relationships(path) | {"
            "source_id: startNode(rel).id, target_id: endNode(rel).id, "
            "relationship: rel.relationship, confidence: rel.confidence, "
            "evidence: rel.evidence, auto_extracted: rel.auto_extracted"
            "}] AS edges, length(path) AS depth"
        )
        rows = self.query(cypher, {"id1": id1, "id2": id2})
        if not rows:
            return None
        row = rows[0]
        return {"depth": row["depth"], "edges": row["edges"]}

    # ------------------------------------------------------------------
    # Phase 1: Co-occurrence edges
    # ------------------------------------------------------------------

    def upsert_cooccurrence(
        self,
        agent: str,
        id1: str,
        id2: str,
        belief: float,
        platform_context: str = None,
        activity_context: str = None,
        topic_context: str = None,
    ):
        """Upsert an undirected COOCCURS relationship between two Memory nodes.

        On create sets all properties; on match updates belief and contexts
        and stamps an updated timestamp.
        """
        cypher = (
            "MATCH (m1:Memory {id: $id1}), (m2:Memory {id: $id2}) "
            "MERGE (m1)-[r:COOCCURS {agent: $agent}]-(m2) "
            "ON CREATE SET r.belief=$belief, r.platform_context=$platform_context, "
            "r.activity_context=$activity_context, r.topic_context=$topic_context, "
            "r.created=datetime() "
            "ON MATCH SET r.belief=$belief, r.platform_context=$platform_context, "
            "r.activity_context=$activity_context, r.topic_context=$topic_context, "
            "r.updated=datetime()"
        )
        self.write(
            cypher,
            {
                "agent": agent,
                "id1": id1,
                "id2": id2,
                "belief": belief,
                "platform_context": platform_context,
                "activity_context": activity_context,
                "topic_context": topic_context,
            },
        )

    def get_cooccurrence(self, agent: str, id1: str, id2: str) -> "dict | None":
        """Return the COOCCURS relationship between two Memory nodes for an agent.

        Returns a dict or None if no relationship exists.
        """
        cypher = (
            "MATCH (m1:Memory {id: $id1})-[r:COOCCURS {agent: $agent}]-(m2:Memory {id: $id2}) "
            "RETURN m1.id AS id1, m2.id AS id2, r.belief AS belief, "
            "r.platform_context AS platform_context, "
            "r.activity_context AS activity_context, "
            "r.topic_context AS topic_context"
        )
        rows = self.query(cypher, {"agent": agent, "id1": id1, "id2": id2})
        return rows[0] if rows else None

    def get_all_cooccurrences(self, agent: str) -> list:
        """Return all COOCCURS relationships for an agent.

        Each record includes id1, id2, belief, and context fields.
        """
        cypher = (
            "MATCH (m1:Memory)-[r:COOCCURS {agent: $agent}]-(m2:Memory) "
            "WHERE id(m1) < id(m2) "
            "RETURN m1.id AS id1, m2.id AS id2, r.belief AS belief, "
            "r.platform_context AS platform_context, "
            "r.activity_context AS activity_context, "
            "r.topic_context AS topic_context"
        )
        return self.query(cypher, {"agent": agent})

    # ------------------------------------------------------------------
    # Phase 1: Observations
    # ------------------------------------------------------------------

    def add_observation(
        self,
        agent: str,
        id1: str,
        id2: str,
        source_type: str,
        session_id: str,
        weight: float = 1.0,
        trust_tier: str = "standard",
        platform: str = None,
        activity: str = None,
        direction_weight: float = 0.5,
    ):
        """Create an Observation node linked to both Memory nodes involved in a COOCCURS edge.

        Because Neo4j does not support relationships to relationships natively,
        the Observation node is connected to m1 via :FROM and to m2 via :TO,
        carrying the agent property so it can be associated with the correct
        COOCCURS edge.
        """
        cypher = (
            "MATCH (m1:Memory {id: $id1}), (m2:Memory {id: $id2}) "
            "CREATE (obs:Observation {"
            "agent: $agent, "
            "source_type: $source_type, "
            "session_id: $session_id, "
            "weight: $weight, "
            "trust_tier: $trust_tier, "
            "platform: $platform, "
            "activity: $activity, "
            "direction_weight: $direction_weight, "
            "created: datetime()"
            "}) "
            "CREATE (m1)<-[:FROM]-(obs)-[:TO]->(m2)"
        )
        self.write(
            cypher,
            {
                "agent": agent,
                "id1": id1,
                "id2": id2,
                "source_type": source_type,
                "session_id": session_id,
                "weight": weight,
                "trust_tier": trust_tier,
                "platform": platform,
                "activity": activity,
                "direction_weight": direction_weight,
            },
        )

    # ------------------------------------------------------------------
    # Phase 1: Stats
    # ------------------------------------------------------------------

    def edge_stats(self, agent: str) -> dict:
        """Return counts of TYPED_EDGE, COOCCURS, and Observation nodes for an agent."""
        typed_rows = self.query(
            "MATCH ()-[r:TYPED_EDGE {agent: $agent}]->() RETURN count(r) AS count",
            {"agent": agent},
        )
        cooccur_rows = self.query(
            "MATCH ()-[r:COOCCURS {agent: $agent}]-() RETURN count(DISTINCT r) AS count",
            {"agent": agent},
        )
        obs_rows = self.query(
            "MATCH (obs:Observation {agent: $agent}) RETURN count(obs) AS count",
            {"agent": agent},
        )
        return {
            "typed_edges": typed_rows[0]["count"] if typed_rows else 0,
            "cooccurrences": cooccur_rows[0]["count"] if cooccur_rows else 0,
            "observations": obs_rows[0]["count"] if obs_rows else 0,
        }


def get_graph() -> GraphDB:
    """Get a GraphDB instance."""
    return GraphDB()
