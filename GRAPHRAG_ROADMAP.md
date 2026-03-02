# GraphRAG Migration Roadmap

## Why Graph-Native Recall Is Superior

The current memory system uses PostgreSQL + pgvector for semantic search. Memories are retrieved by embedding similarity — "find chunks that look like this query." This works for specific recall ("what happened in the Terrance debate?") but fails at structural reasoning:

- **"What patterns connect Gerald's fraud findings to Max's crypto analysis?"** — requires multi-hop traversal across two agents' knowledge graphs
- **"Which topics create the most productive debates?"** — requires community detection + centrality analysis
- **"What does this agent actually believe about AI regulation?"** — requires aggregating contradictory memories into coherent belief clusters

The system already builds graph edges (edges_v3, typed_edges, co_occurrences, context_graphs) but queries them with SQL joins and recursive CTEs. This is like having a map but navigating by reading the street index instead of looking at it.

### What GraphRAG Adds

Microsoft's GraphRAG pattern (Darren Edge et al., 2024) solves this with two key ideas:

1. **Community Detection**: Run Leiden algorithm on the knowledge graph to find natural clusters of related memories. Each cluster becomes a "knowledge community" — e.g., "fraud detection techniques", "debate strategy", "crypto market patterns."

2. **Hierarchical Summarization**: Each community gets an LLM-generated summary at multiple abstraction levels. Level 0 = individual memories. Level 1 = topic clusters. Level 2 = domain themes. Level 3 = agent worldview.

**Recall then becomes**: embed query → identify relevant communities → pull community summaries for context → drill into specific memories within those communities. This gives agents genuine "understanding" rather than pattern-matching against fragments.

### Concrete Improvements

| Query Type | Current (pgvector) | With GraphRAG |
|---|---|---|
| "What happened in debate X?" | Good — direct embedding match | Same |
| "What patterns do you see in fraud data?" | Poor — returns random fraud-adjacent chunks | Pulls fraud community summary + connected evidence chains |
| "How has your thinking on AI evolved?" | Very poor — returns scattered mentions | Traverses temporal-causal subgraph, shows belief evolution |
| "What do you and Max disagree about?" | Fails — can't cross-agent reason | Cross-community comparison with typed contradiction edges |
| "Summarize everything you know" | Impossible at scale | Hierarchical community summaries at each level |

## Current Architecture (What We Have)

```
PostgreSQL 16 + pgvector
├── memories          — content + metadata + importance + q-values
├── text_embeddings   — halfvec(1024) HNSW index (Qwen3-Embedding)
├── edges_v3          — provenance-based co-occurrence (belief-weighted)
├── typed_edges       — semantic relationships (causes, enables, contradicts, etc.)
├── co_occurrences    — raw pair counts
├── context_graphs    — 5W dimensional projections (WHO/WHAT/WHY/WHERE/WHEN)
├── lessons           — extracted behavioral rules
├── sessions          — session tracking + recall audit
└── key_value_store   — state persistence
```

**Recall pipeline**: embed query → HNSW cosine search → entity index injection → Q-value re-ranking → dimensional boosting → output

**What's already graph-like**: edges_v3 (belief-weighted edges with provenance), typed_edges (16 relationship types with confidence), context_graphs (5W dimensional projections with hub detection), knowledge_graph.py (multi-hop traversal via recursive CTE).

We're doing graph operations in a relational DB. Time to use a graph DB.

## Target Architecture (Parallel Operation)

```
                    WRITE PATH (latency-critical)
                    ┌──────────────────────────┐
Session ──────────► │ PostgreSQL (source of truth) │
                    │  memories, embeddings,     │
                    │  edges, sessions, decay    │
                    └───────────┬───────────────┘
                                │ async sync
                                ▼
                    ┌──────────────────────────┐
                    │ Neo4j (graph engine)       │
                    │  nodes = memories          │
                    │  relationships = edges     │
                    │  communities = auto-detect │
                    │  summaries = per-community │
                    └───────────┬───────────────┘
                                │
                    READ PATH (recall)
                    ┌───────────┴───────────────┐
                    │ GraphRAG Retrieval          │
                    │  1. Embed query             │
                    │  2. Identify communities    │
                    │  3. Pull community summaries│
                    │  4. Drill into specifics    │
                    │  5. Cross-community links   │
                    └──────────────────────────────┘
```

**Key principle**: PostgreSQL remains source of truth for all writes. Neo4j is a read-optimized graph projection that's rebuilt/synced asynchronously. If Neo4j goes down, the system degrades to current pgvector recall — not a hard failure.

## Migration Phases

### Phase 0 — Neo4j Alongside PostgreSQL (Week 1)
**Goal**: Get Neo4j running, mirror existing data, prove it works.

- [ ] Add Neo4j to docker-compose.yml (neo4j:community with APOC + GDS plugins)
- [ ] Write `shared/drift-memory/neo4j_adapter.py` — connection pool, basic Cypher helpers
- [ ] Write `shared/drift-memory/graph_sync.py` — PostgreSQL → Neo4j incremental sync
  - Memories → `(:Memory {id, content, type, importance, q_value, created})`
  - edges_v3 → `(:Memory)-[:COOCCURS {belief, first_formed}]->(:Memory)`
  - typed_edges → `(:Memory)-[:CAUSES|ENABLES|CONTRADICTS|... {confidence}]->(:Memory)`
  - Shared memories → `(:SharedMemory)` with `:CREATED_BY` edges to agents
- [ ] Run initial full sync of all agent schemas
- [ ] Add sync hook to sleep phase in memory_wrapper.py (non-blocking, fire-and-forget)
- [ ] Verify: `MATCH (n:Memory) RETURN count(n)` matches PostgreSQL memory counts

**Risk**: Low — Neo4j is purely additive, nothing changes in the existing pipeline.

### Phase 1 — Community Detection (Week 2)
**Goal**: Discover knowledge communities in each agent's memory graph.

- [ ] Install Neo4j Graph Data Science (GDS) library
- [ ] Write `shared/drift-memory/community_detection.py`:
  - Run Leiden algorithm on each agent's memory graph
  - `CALL gds.leiden.stream('memory-graph', {relationshipWeightProperty: 'belief'})`
  - Assign community IDs to memory nodes
  - Track community evolution across sessions (which communities grow/shrink/split)
- [ ] Generate community metadata:
  - Top entities per community
  - Dominant relationship types
  - Temporal span (oldest → newest memory)
  - Average importance / q-value
- [ ] Visualize: Export community structure for dashboard.py

**Deliverable**: Each agent's memories organized into 10-50 natural knowledge communities.

### Phase 2 — Hierarchical Summarization (Week 2-3)
**Goal**: Build multi-level summaries for each community.

- [ ] Write `shared/drift-memory/community_summarizer.py`:
  - Level 0: Individual memory content (already exists)
  - Level 1: Per-community summary (LLM-generated from member memories)
  - Level 2: Domain themes (clusters of related communities)
  - Level 3: Agent worldview (top-level beliefs + stances)
- [ ] Use local Ollama model for summarization (qwen3:latest or kimi when available)
- [ ] Store summaries in Neo4j as `(:CommunitySummary)` nodes linked to communities
- [ ] Re-summarize incrementally: when a community gains >3 new memories since last summary
- [ ] Add to sleep phase: after memory consolidation, trigger community re-detection + re-summarization for affected communities

**Deliverable**: Queryable hierarchical knowledge map per agent.

### Phase 3 — GraphRAG Retrieval (Week 3-4)
**Goal**: Replace embedding-only recall with community-aware retrieval.

- [ ] Write `shared/drift-memory/graphrag_retrieval.py`:
  - **Local search** (specific queries): embed query → vector search → find which communities the results belong to → pull community summary for context → return memories + community context
  - **Global search** (broad queries): embed query → match against community summaries → return relevant community summaries + representative memories
  - **Map-reduce** (complex queries): fan out to multiple communities → collect partial answers → reduce into coherent response
- [ ] Integrate into `memory_wrapper.py` wake phase:
  - New mode: `wake_graphrag` alongside existing `wake` and `wake_cue`
  - Falls back to pgvector if Neo4j unavailable
- [ ] A/B testing: log both pgvector and GraphRAG results, compare quality
- [ ] Tune: community granularity, summary detail level, traversal depth

**Deliverable**: Agents recall with structural understanding, not just similarity matching.

### Phase 4 — Cross-Agent Graph (Week 4-5)
**Goal**: Unified multi-agent knowledge graph.

- [ ] Merge agent subgraphs into unified Neo4j graph with agent labels
- [ ] Cross-agent community detection: find where agents' knowledge overlaps/contradicts
- [ ] Shared community summaries: "What the collective knows about X"
- [ ] Debate-informed edges: when agents debate, create explicit agreement/disagreement edges
- [ ] Cross-agent retrieval: "What do other agents know about this topic?" queries the unified graph
- [ ] Contradiction detection: find where agents hold contradictory beliefs (different communities, opposing typed_edges)

**Deliverable**: Agents can reason about what others know and where they disagree.

### Phase 5 — PostgreSQL Sunset (Week 6+)
**Goal**: Evaluate whether Neo4j can become source of truth.

- [ ] Benchmark: Neo4j write latency vs PostgreSQL for session-critical path
- [ ] Evaluate: Can Neo4j vector index replace pgvector? (Neo4j 5.x has native vector search)
- [ ] If latency acceptable: migrate writes to Neo4j, keep PostgreSQL as backup
- [ ] If not: keep dual-write architecture permanently (PostgreSQL for speed, Neo4j for intelligence)
- [ ] Either way: the GraphRAG retrieval layer stays on Neo4j

**Decision point**: Full migration vs permanent dual-write depends on Neo4j write performance under load.

## Docker Setup

```yaml
# Add to docker-compose.yml
neo4j:
  image: neo4j:5-community
  container_name: drift-agents-graph
  ports:
    - "7474:7474"   # Browser UI
    - "7687:7687"   # Bolt protocol
  environment:
    NEO4J_AUTH: neo4j/drift_graph_local
    NEO4J_PLUGINS: '["graph-data-science", "apoc"]'
    NEO4J_dbms_memory_heap_max__size: 1G
    NEO4J_dbms_memory_pagecache_size: 512M
  volumes:
    - ./neo4jdata:/data
    - ./neo4jlogs:/logs
  healthcheck:
    test: ["CMD", "cypher-shell", "-u", "neo4j", "-p", "drift_graph_local", "RETURN 1"]
    interval: 10s
    timeout: 5s
    retries: 5
```

## File Plan

```
shared/drift-memory/
├── neo4j_adapter.py          # Connection pool, Cypher helpers
├── graph_sync.py             # PostgreSQL → Neo4j incremental sync
├── community_detection.py    # Leiden algorithm, community tracking
├── community_summarizer.py   # Hierarchical LLM summaries
├── graphrag_retrieval.py     # Local/global/map-reduce retrieval
└── graphrag_config.py        # Tuning parameters
```

## Why Not Just Use PostgreSQL AGE?

Apache AGE adds Cypher queries to PostgreSQL — tempting because it avoids a new database. But:

1. **No GDS**: AGE has no graph algorithms library. No Leiden, no PageRank, no community detection. You'd have to implement these yourself or pull data into Python.
2. **No native graph storage**: AGE stores graphs as tables. Traversal still goes through the PostgreSQL query planner, which isn't optimized for recursive graph walks.
3. **No vector + graph**: Neo4j 5.x has native vector search, so eventually we could do embedding similarity AND graph traversal in a single query.
4. **Ecosystem**: Neo4j has GraphRAG implementations, LangChain integration, visualization tools. AGE has a Cypher parser.

AGE is fine for simple graph queries. For real GraphRAG with community detection and hierarchical summarization, you need the graph algorithm ecosystem that only Neo4j GDS provides.

## Success Metrics

1. **Recall relevance**: Side-by-side comparison of pgvector vs GraphRAG results for 50 test queries
2. **Global query capability**: Can agents answer "what patterns do you see across all debates?" (currently impossible)
3. **Cross-agent reasoning**: Can agents reference what other agents know without being told?
4. **Community stability**: Do detected communities remain coherent across sessions?
5. **Latency**: GraphRAG recall completes within 2s (current pgvector: ~200ms, budget for graph overhead)

## Dependencies

- Neo4j Community Edition 5.x (free, Docker)
- neo4j Python driver (`pip install neo4j`)
- Neo4j Graph Data Science plugin (free for community)
- APOC plugin (utility procedures)
- Existing: PostgreSQL, pgvector, Ollama (embeddings + summarization)
