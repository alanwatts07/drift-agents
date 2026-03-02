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
        ]
        for cypher in constraints:
            try:
                self.write(cypher)
            except Exception:
                pass  # Constraint may already exist


def get_graph() -> GraphDB:
    """Get a GraphDB instance."""
    return GraphDB()
