# Plan: Clean DB Split — Postgres for Tables, Neo4j for Graphs

## Current State

Right now edges are stored in **both** databases:

```
PostgreSQL (source of truth)          Neo4j (read-only mirror)
├── edges_v3 (co-occurrence)    ──→   :COOCCURS relationships
├── typed_edges (semantic)      ──→   :CAUSES, :ENABLES, etc.
├── edge_observations           ──→   (not synced)
├── co_occurrences (legacy v2)  ──→   (not synced)
└── memories, sessions, KV...         :Memory nodes, :Community nodes
```

Writes always go to Postgres. `graph_sync.py` batch-copies to Neo4j. Neo4j then does community detection and graph retrieval. This means:

- Every edge is stored twice
- Sync can drift out of date
- graph_sync.py is a maintenance burden
- Co-occurrence writes are Postgres-optimized (upserts, belief aggregation) but the *reads* are Neo4j-optimized (traversal, community detection)

## Target State

```
PostgreSQL (tabular data)             Neo4j (all graph data)
├── memories (CRUD, bulk ops)         ├── :Memory nodes (synced from PG)
├── text_embeddings (pgvector)        ├── :COOCCURS relationships ← WRITES HERE NOW
├── key_value_store                   ├── :CAUSES, :ENABLES, etc. ← WRITES HERE NOW
├── sessions / session_recalls        ├── :Community nodes (Leiden)
├── q_value_history                   ├── :Observation metadata (on edges)
├── decay_history                     ├── :SharedMemory, :Lesson nodes
├── lessons                           └── All traversal, expansion, community queries
└── context_graphs

REMOVED FROM POSTGRES:
├── edges_v3          → Neo4j :COOCCURS
├── typed_edges       → Neo4j typed relationships
├── edge_observations → Neo4j edge properties/observation nodes
├── co_occurrences    → DELETED (legacy v2, superseded by edges_v3)
```

## Why This Split

| Operation | Best DB | Why |
|-----------|---------|-----|
| Memory CRUD, tier promotion, bulk decay | Postgres | Batch UPDATEs, transactions, WHERE clauses |
| Vector similarity search | Postgres (pgvector) | Mature HNSW, halfvec, battle-tested |
| KV store (affect, goals, narrative) | Postgres | Fast key lookups, JSONB |
| Time-series (Q-history, sessions) | Postgres | Ordered inserts, range queries |
| Co-occurrence edges + belief | **Neo4j** | Graph-native upserts, traversal |
| Typed semantic edges | **Neo4j** | Relationship types are first-class |
| Community detection | **Neo4j** | Already runs here (Leiden on graph structure) |
| Graph expansion / traversal | **Neo4j** | 1-hop, multi-hop, BFS — this is what it's built for |
| Edge observations / provenance | **Neo4j** | Properties on relationships, or linked observation nodes |

---

## Migration Phases

### Phase 1: Create Neo4j Write Layer (neo4j_adapter.py) ✅ COMPLETE — 2026-03-07

**Goal:** Add write methods to `neo4j_adapter.py` that mirror what `co_occurrence.py` and `knowledge_graph.py` currently do via SQL.

**New methods on `GraphDB`:**

```python
# Co-occurrence edges
def upsert_cooccurrence(self, agent, id1, id2, belief_delta, context: dict)
def get_cooccurrence(self, id1, id2) -> dict | None
def get_all_cooccurrences(self, agent) -> list
def batch_decay_cooccurrences(self, agent, rate, exclude_pairs=None)
def prune_weak_cooccurrences(self, agent, threshold)

# Typed edges
def upsert_typed_edge(self, agent, source_id, target_id, relationship, confidence, evidence, auto_extracted)
def get_typed_edges_from(self, source_id, relationship=None) -> list
def get_typed_edges_to(self, target_id, relationship=None) -> list
def delete_typed_edge(self, source_id, target_id, relationship)

# Observations (as properties on edges or linked nodes)
def add_observation(self, id1, id2, source_type, weight, trust_tier, session_id, agent, platform)
def get_observations(self, id1, id2) -> list
def aggregate_belief(self, id1, id2) -> float

# Homeostasis
def synaptic_homeostasis(self, agent, threshold)

# Stats
def edge_stats(self, agent) -> dict
```

**Design decision — observations:** Two options:
1. **Properties on relationships** — simpler, but loses individual observation history
2. **Linked `:Observation` nodes** — `(m1)-[:COOCCURS]->(m2)<-[:OBSERVED_AT]-(obs:Observation {source_type, weight, trust_tier, timestamp})`. Preserves full provenance for belief re-aggregation.

Recommend option 2 since the belief aggregation formula needs individual observations.

**Files changed:** `shared/graphrag/neo4j_adapter.py`
**Files created:** None (extend existing)
**Risk:** Low — additive only, nothing removed yet

---

### Phase 2: Dual-Write Bridge ✅ COMPLETE — 2026-03-07

**Goal:** `co_occurrence.py` and `knowledge_graph.py` write to **both** Postgres and Neo4j simultaneously. This lets us validate Neo4j writes match Postgres without breaking anything.

**Changes:**

**`co_occurrence.py`:**
- `log_co_occurrences_v3()` — after PG upsert, also call `graph.upsert_cooccurrence()`
- `add_observation()` — after PG insert, also call `graph.add_observation()`
- `decay_pair_cooccurrences_v3()` — after PG update, also call `graph.batch_decay_cooccurrences()`
- `synaptic_homeostasis_v3()` — after PG normalize, also call `graph.synaptic_homeostasis()`

**`knowledge_graph.py`:**
- `add_edge()` — after PG upsert, also call `graph.upsert_typed_edge()`
- `extract_from_memory()` — same (goes through `add_edge()`)
- `delete_edge()` — after PG delete, also call `graph.delete_typed_edge()`

**Pattern:**
```python
def add_edge(source_id, target_id, relationship, ...):
    # Existing PG write (keep for now)
    db = get_db()
    db.upsert_typed_edge(source_id, target_id, relationship, ...)

    # NEW: Also write to Neo4j
    try:
        from graphrag.neo4j_adapter import get_graph
        graph = get_graph()
        graph.upsert_typed_edge(agent, source_id, target_id, relationship, ...)
    except Exception as e:
        print(f"[kg] Neo4j write failed (non-fatal): {e}", file=sys.stderr)
```

Neo4j writes are wrapped in try/except so if Neo4j is down, Postgres still works.

**Validation:** Run a sync cycle, then compare edge counts and belief values between PG and Neo4j. They should match.

**Files changed:** `co_occurrence.py`, `knowledge_graph.py`
**Risk:** Low — PG writes unchanged, Neo4j writes are additive + non-fatal

---

### Phase 3: Switch Reads to Neo4j ✅ COMPLETE — 2026-03-07

**Goal:** All edge *reads* come from Neo4j instead of Postgres. This is where the graph queries actually get faster.

**Changes:**

**`knowledge_graph.py`:**
- `get_edges_from()` → Cypher: `MATCH (m:Memory {id: $id})-[r]->(target) RETURN ...`
- `get_edges_to()` → Cypher: `MATCH (source)-[r]->(m:Memory {id: $id}) RETURN ...`
- `get_all_edges()` → Cypher: `MATCH (m:Memory {id: $id})-[r]-(other) RETURN ...`
- `batch_get_edges()` → Cypher: `UNWIND $ids AS id MATCH (m:Memory {id: id})-[r]-(other) RETURN ...`
- `traverse()` → Cypher: `MATCH path = (start:Memory {id: $id})-[*1..N]-(end) RETURN ...` (replaces recursive CTE)
- `find_path()` → Cypher: `MATCH path = shortestPath((a:Memory {id: $id1})-[*..N]-(b:Memory {id: $id2})) RETURN ...`
- `get_stats()` → Cypher aggregate queries

**`co_occurrence.py`:**
- `decay_pair_cooccurrences_v3()` reads all edges → switch to `graph.get_all_cooccurrences()`
- Observation reads for belief re-aggregation → `graph.get_observations()`

**`db_adapter.py`:**
- `get_neighbors()` → delegate to Neo4j (or remove, since knowledge_graph.py handles this)

**graph_sync.py:**
- Remove `sync_agent_cooccurrences()` and `sync_agent_typed_edges()` — no longer needed since Neo4j is now the source of truth for edges
- Keep `sync_agent_memories()` — PG memories still need to sync as nodes

**Files changed:** `knowledge_graph.py`, `co_occurrence.py`, `db_adapter.py`, `graph_sync.py`
**Risk:** Medium — changing read paths. Validate by comparing query results before/after.

---

### Phase 4: Drop Postgres Edge Tables

**Goal:** Remove the now-unused Postgres tables and all SQL that writes to them.

**Drop tables:**
```sql
DROP TABLE IF EXISTS {agent}.edges_v3 CASCADE;
DROP TABLE IF EXISTS {agent}.typed_edges CASCADE;
DROP TABLE IF EXISTS {agent}.edge_observations CASCADE;
DROP TABLE IF EXISTS {agent}.co_occurrences CASCADE;
```

**Code cleanup:**
- `co_occurrence.py` — remove all `db.` SQL calls for edges, keep only Neo4j calls
- `knowledge_graph.py` — remove all PG SQL, keep only Neo4j calls
- `db_adapter.py` — remove `get_neighbors()`, any edge-related methods
- `graph_sync.py` — remove edge sync functions, keep memory node sync only
- `init_schema.sql` — remove edge table definitions from `create_agent_schema()`

**Files changed:** `co_occurrence.py`, `knowledge_graph.py`, `db_adapter.py`, `graph_sync.py`, `init_schema.sql`
**Risk:** Medium — destructive. Take a pg_dump backup first.

---

### Phase 5: Kill Legacy v2

**Goal:** Remove the `co_occurrences` table (legacy bidirectional counters) entirely. It's already superseded by `edges_v3`.

This should already be dead after Phase 4, but audit for any remaining references:
- `log_co_occurrences()` (v2 function) — delete
- `decay_pair_cooccurrences()` (v2 decay) — delete
- Any code that reads `co_occurrences` table — delete

---

## Validation Checklist (Per Phase)

- [ ] Agent wake retrieves same quality memories (semantic hits unchanged — pgvector untouched)
- [ ] GraphRAG expansion returns same/better results (now reading from authoritative source)
- [ ] Community detection still works (reads edges from Neo4j — same as before)
- [ ] Co-occurrence belief values match between PG and Neo4j (Phase 2 dual-write)
- [ ] Typed edge counts match (Phase 2)
- [ ] `traverse()` and `find_path()` return equivalent results via Cypher vs recursive CTE (Phase 3)
- [ ] Sleep phase correctly writes new edges to Neo4j (Phase 2+)
- [ ] Decay and homeostasis operate correctly on Neo4j (Phase 3)
- [ ] No PG queries reference dropped tables (Phase 4)
- [ ] `python memory_wrapper.py status <agent>` still works (edge count now from Neo4j)

## Timeline Estimate

| Phase | Effort | Dependencies |
|-------|--------|-------------|
| Phase 1: Neo4j write layer | 1-2 days | None |
| Phase 2: Dual-write bridge | 1 day | Phase 1 |
| Phase 3: Switch reads | 2-3 days | Phase 2 validated |
| Phase 4: Drop PG tables | Half day | Phase 3 validated |
| Phase 5: Kill legacy v2 | Half day | Phase 4 |

**Total: ~5-7 days of focused work.**

Each phase is independently deployable. You can stop after any phase and have a working system. The agents keep running the whole time.

## What This Does NOT Change

- pgvector / semantic search (stays in Postgres)
- KV store — affect, goals, narrative, cognitive state (stays in Postgres)
- Sessions, Q-value history, decay history (stays in Postgres)
- Memory CRUD — insert, update, tier promotion (stays in Postgres)
- Memory node sync (PG → Neo4j via graph_sync, kept)
- Community detection pipeline (already Neo4j-native, unchanged)
