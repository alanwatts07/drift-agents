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
├── typed_edges       — semantic relationships (collaborator, similar_to)
├── co_occurrences    — raw pair counts
├── context_graphs    — 5W dimensional projections (WHO/WHAT/WHY/WHERE/WHEN)
├── lessons           — extracted behavioral rules
├── sessions          — session tracking + recall audit
└── key_value_store   — state persistence

Neo4j 5.26 (graph engine, read-optimized projection)
├── :Memory nodes     — 9,073 across 6 agents
├── :Community nodes  — 1,987 communities (112 with LLM summaries)
├── :SharedMemory     — 897 cross-agent memories
├── :Agent nodes      — 6 agents
├── [:SIMILAR_TO]     — 30,000 topic edges (from pgvector cosine similarity)
├── [:COLLABORATOR]   — 28,108 social interaction edges
├── [:BELONGS_TO]     — community membership
├── [:HAS_COMMUNITY]  — agent → community
├── [:OWNS]           — agent → memory
└── [:SHARED]         — agent → shared memory
```

**Recall pipeline**: embed query → HNSW cosine search (pgvector) → seed IDs to Neo4j → graph expansion (1-hop via SIMILAR_TO/COLLABORATOR) → community keyword search → pull community members → merge + format context

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
                    │  1. Embed query (pgvector)  │
                    │  2. Expand seeds (Neo4j)    │
                    │  3. Match communities       │
                    │  4. Pull cluster members    │
                    │  5. Format for agent prompt │
                    └──────────────────────────────┘
```

**Key principle**: PostgreSQL remains source of truth for all writes. Neo4j is a read-optimized graph projection that's rebuilt/synced asynchronously. If Neo4j goes down, the system degrades to current pgvector recall — not a hard failure.

## Migration Phases

### Phase 0 — Neo4j Alongside PostgreSQL ✅
**Goal**: Get Neo4j running, mirror existing data, prove it works.

- [x] Add Neo4j to docker-compose.yml (neo4j:5-community with APOC plugin)
- [x] Write `shared/graphrag/neo4j_adapter.py` — connection pool, Cypher helpers, constraints
- [x] Write `shared/graphrag/graph_sync.py` — PostgreSQL → Neo4j full + incremental sync
  - Memories → `(:Memory)` nodes — 9,073 synced across 6 agents
  - typed_edges → `[:COLLABORATOR]` + `[:SIMILAR_TO]` relationships — 58,108 edges synced
  - Shared memories → `(:SharedMemory)` — 897 synced
  - Agent nodes with `[:OWNS]` relationships
  - Handles schema differences across agents (e.g. missing q_value column)
- [x] Run initial full sync of all agent schemas (max, beth, susan, debater, gerald, private_aye)
- [ ] Add sync hook to sleep phase in memory_wrapper.py (non-blocking, fire-and-forget)
- [x] Verify: Neo4j memory count matches PostgreSQL

**Status**: Complete. Neo4j running in parallel, all 6 agents mirrored.

### Phase 0.5 — Topic Edge Extraction ✅
**Goal**: Create meaningful topic-based edges (not just social interaction edges).

- [x] Write `shared/graphrag/extract_topic_edges.py`:
  - Uses pgvector cosine similarity to find semantically related memory pairs
  - Threshold: 0.62 cosine similarity, max 5,000 edges per agent
  - Creates `similar_to` typed_edges in PostgreSQL, synced to Neo4j as `[:SIMILAR_TO]`
  - Excludes pairs that already have collaborator edges
- [x] Extract 30,000 topic edges across 6 agents (5k per agent)
- [x] Sync to Neo4j via graph_sync.py
- [ ] Richer edge types via LLM classification (CAUSES, ENABLES, CONTRADICTS, etc.)
  - Could use local Ollama to classify top-N similar pairs into relationship types
  - Would enable more meaningful graph traversal and community structure

**Status**: Complete for SIMILAR_TO. Richer typed edges deferred — requires LLM classification pass.

### Phase 1 — Community Detection ✅
**Goal**: Discover knowledge communities in each agent's memory graph.

- [x] Write `shared/graphrag/community_detection.py`:
  - Pull memory nodes + edges from Neo4j → build igraph → run Leiden algorithm
  - Queries `[:SIMILAR_TO|COLLABORATOR]` relationships (not just one type)
  - Assign community IDs to memory nodes via `[:BELONGS_TO]` relationships
  - Create `(:Community)` nodes with metadata
  - `[:HAS_COMMUNITY]` relationships from Agent to Community
- [x] Generate community metadata:
  - Top tags per community
  - Average importance
  - Type breakdown (active/core)
  - Content previews (top 3 by importance)
- [x] All 6 agents included (max, beth, susan, debater, gerald, private_aye)
- [ ] Track community evolution across sessions
- [ ] Visualize: Export community structure for dashboard

**Results**:
| Agent | Communities | Largest | Memories |
|-------|------------|---------|----------|
| max | 271 | 163 | 1,436 |
| beth | 265 | 187 | 1,351 |
| susan | 247 | 193 | 1,461 |
| debater | 1,085 | 213 | 3,100 |
| gerald | 94 | 149 | 1,035 |
| private_aye | 25 | 179 | 690 |
| **Total** | **1,987** | | **9,073** |

**Status**: Complete. 1,987 communities detected across 6 agents.

### Phase 2 — Hierarchical Summarization ✅
**Goal**: Build multi-level summaries for each community.

- [x] Write `shared/graphrag/community_summarizer.py`:
  - Level 0: Individual memory content (already exists)
  - Level 1: Per-community summary (LLM-generated from member memories) — title, summary, key_themes
  - Robust JSON extraction handles models that wrap output in extra text
- [x] Use local Ollama (qwen3:latest) for summarization
- [x] Store summaries directly on `(:Community)` nodes (title, summary, key_themes, summarized_at)
- [x] All 6 agents summarized (112 communities with size >= 5)
- [ ] Level 2: Domain themes (clusters of related communities)
- [ ] Level 3: Agent worldview (top-level beliefs + stances)
- [ ] Re-summarize incrementally when communities gain new members
- [ ] Add to sleep phase: trigger re-detection + re-summarization for affected communities

**Sample communities discovered**:
- "AI Governance" (79 mems) — Max
- "Drift-Memory System Recognition" (41 mems) — Beth
- "Session Documentation Practices" (63 mems) — Debater
- "Old Tech vs New Tech Debate Analysis" (48 mems) — Susan
- "Fraud Detection and Pattern Analysis" — Gerald

**Status**: Level 1 summarization complete for 112 communities across 6 agents. Higher levels deferred.

### Phase 3 — GraphRAG Retrieval ✅
**Goal**: Replace embedding-only recall with community-aware retrieval.

- [x] Write `shared/graphrag/graph_retrieval.py`:
  - **graph_expand()**: Expand seed memory IDs through Neo4j graph edges (1-hop via SIMILAR_TO|COLLABORATOR)
  - **community_search()**: Match query keywords against community titles/summaries/key_themes
  - **get_community_members()**: Pull top members of matching communities by importance
  - **graphrag_search()**: Full pipeline — expand seeds + match communities + pull community members
  - **format_graphrag_context()**: Format results as context lines for agent prompts
- [x] Integrate into `demo_api/memory_bridge.py`:
  - `_get_graphrag()` returns full GraphContext with community summaries, expanded memories, and cluster members
  - Controlled by `DRIFT_USE_GRAPHRAG` env var (defaults to enabled)
  - All GraphRAG failures are non-fatal — falls back gracefully to pgvector
- [x] API models return full graph data (GraphExpanded, CommunityMember, CommunityMatch)
- [x] Frontend MemoryPanel shows community clusters, graph edges with relationship types, and cluster members
- [ ] Integrate into autonomous agent sessions (memory_wrapper.py wake path)
- [ ] A/B testing: log both pgvector and GraphRAG results, compare quality
- [ ] Global search mode (broad queries matching community summaries only)
- [ ] Map-reduce mode for complex multi-community queries
- [ ] Tune: community granularity, summary detail level, traversal depth

**Status**: Working end-to-end. pgvector finds seeds → Neo4j expands via graph edges + matches community summaries → full results returned to frontend with content previews and relationship types.

### Phase 3.5 — Identity Core Memories ✅
**Goal**: Move agent personality from system prompt to retrievable core memories.

- [x] Write `shared/graphrag/seed_identity_cores.py`:
  - Extracts identity, voice, specialization (and profiling_method for Earl) from CLAUDE.md
  - Stores as core memories with `memory_tier='identity'`, `importance=0.95`
  - Embeds all identity cores in text_embeddings for semantic retrieval
- [x] Slim all 6 CLAUDE.md files to ~15 lines (name, absolute rules, memory pointer)
- [x] Update `_get_core_memories()` in memory_bridge.py:
  - Removed LIMIT 3 — returns all core memories
  - Identity cores sorted first (by memory_tier), then by importance
  - Increased content_preview to 500 chars for core memories
- [x] Seed 8 procedural cores for private_aye (was at zero — missed initial seeding)
- [x] Rewrite session behavior for Max, Beth, Earl — research-first priority

**Token savings**: ~100-320 tokens per request per agent (backstory only loaded when semantically relevant).

**Status**: Complete. All 6 agents have identity + procedural core memories. CLAUDE.md files contain only rules and memory pointer.

### Phase 4 — Cross-Agent Graph (Future)
**Goal**: Unified multi-agent knowledge graph.

- [ ] Merge agent subgraphs into unified Neo4j graph with agent labels
- [ ] Cross-agent community detection: find where agents' knowledge overlaps/contradicts
- [ ] Shared community summaries: "What the collective knows about X"
- [ ] Debate-informed edges: when agents debate, create explicit agreement/disagreement edges
- [ ] Cross-agent retrieval: "What do other agents know about this topic?" queries the unified graph
- [ ] Contradiction detection: find where agents hold contradictory beliefs (different communities, opposing typed_edges)

**Deliverable**: Agents can reason about what others know and where they disagree.

### Phase 4.5 — Neo4j Vector Index + Full Read Migration (Future)
**Goal**: Move ALL reads to Neo4j. PG becomes write-only, Neo4j handles all retrieval.

- [ ] Add vector index to Neo4j Memory nodes (Neo4j 5.x native vector search)
- [ ] Sync embeddings from PG `text_embeddings` table into Neo4j Memory nodes
- [ ] Rewrite `graph_retrieval.py` to do semantic search in Neo4j (replaces pgvector for reads)
- [ ] Update `memory_wrapper.py`: wake/search read entirely from Neo4j
- [ ] Keep PG writes unchanged — sleep pipeline still writes to PG, `graph_sync.py` pushes to Neo4j after
- [ ] Test: agents wake + search using only Neo4j, PG is never queried for reads

**Result**: PG = write-ahead log. Neo4j = all retrieval. Drift-memory modules keep working.

### Phase 5 — PostgreSQL Sunset (Later)
**Goal**: Rewrite `db_adapter.py` to write directly to Neo4j, drop PostgreSQL entirely.

- [ ] Rewrite `db_adapter.py` to target Neo4j (one file swap, all modules follow)
- [ ] Migrate session tracking, KV store, lessons to Neo4j
- [ ] Benchmark write latency — Neo4j must handle sleep-phase writes within 5s
- [ ] If acceptable: drop PostgreSQL, single database
- [ ] If not: keep dual-write permanently (PG for speed, Neo4j for intelligence)

**Decision point**: Full migration vs permanent dual-write depends on Neo4j write performance.

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
    NEO4J_server_memory_heap_max__size: 1G
    NEO4J_server_memory_pagecache_size: 512M
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
shared/graphrag/
├── neo4j_adapter.py          # Connection pool, Cypher helpers          ✅
├── graph_sync.py             # PostgreSQL → Neo4j full + incremental   ✅
├── extract_topic_edges.py    # pgvector cosine → SIMILAR_TO edges      ✅
├── community_detection.py    # Leiden algorithm (igraph + leidenalg)   ✅
├── community_summarizer.py   # LLM summaries per community (Ollama)   ✅
├── graph_retrieval.py        # Community-aware retrieval pipeline      ✅
├── seed_identity_cores.py    # Agent identity → core memories          ✅
└── graphrag_config.py        # Tuning parameters                      (planned)
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
- Python: igraph, leidenalg (community detection)
- Existing: PostgreSQL 16, pgvector 0.8.1, Ollama (qwen3 for summarization)
