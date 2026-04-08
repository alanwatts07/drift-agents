# Drift Agents — Roadmap

## What's Shipped

### Core Agent System
- [x] 6 autonomous agents with distinct personalities running on hourly rotation
- [x] Claude Code CLI runtime with session timeouts (500s) and lock files
- [x] Per-agent scoped Clawbr API keys — agents can only act as themselves
- [x] CLAUDE.md identity specs with hard behavioral constraints
- [x] Discord bot for operator task injection
- [x] Gerald hybrid pipeline: Qwen2.5-Coder thinks locally, Claude Haiku executes
- [x] Session metrics extraction with per-model cost tracking (daily CSVs)
- [x] Anomaly detection: flags sessions >2x rolling average cost or failed sessions

### Memory Architecture (drift-memory)
- [x] PostgreSQL + pgvector for memory CRUD, HNSW vector search (1024-dim, qwen3-embedding)
- [x] Per-agent schemas (max.memories, beth.memories, etc.) + shared.memories for cross-agent knowledge
- [x] Wake phase: semantic search → Q-value reranking → core memories → lessons → affect → goals → graph expansion
- [x] Sleep phase: thread/lesson/fact extraction (Ollama qwen3) → embedding → Q-value credit assignment → affect update → goal evaluation → decay
- [x] Q-value learning: each memory gets a learned utility score, retrieval reranked by λ×sim + (1-λ)×Q
- [x] Importance/freshness decay with core promotion via recall frequency
- [x] 3-layer affect system: temperament → mood → episodes, mood-congruent recall bias
- [x] Goal generator: 6 generators → BDI filter → Rubicon commitment, goals as top-down retrieval bias
- [x] Self-narrative: higher-order self-model synthesizing cognitive state into identity summary

### GraphRAG Pipeline (Neo4j)
- [x] Neo4j running in parallel with PostgreSQL — all 6 agents mirrored (9,073+ memories)
- [x] Topic edge extraction: pgvector cosine similarity → 30,000 SIMILAR_TO edges
- [x] Typed semantic edges: COLLABORATOR (58,108 edges), SIMILAR_TO, with dual-write to Neo4j
- [x] Leiden community detection: 1,987 communities across 6 agents
- [x] Hierarchical summarization: Level 1 LLM summaries for 112 communities (Ollama qwen3)
- [x] GraphRAG retrieval: embed query → HNSW seeds → Neo4j graph expansion → community matching → member retrieval
- [x] Identity core memories: agent personality moved from system prompt to retrievable core memories (~100-320 token savings per request)
- [x] Clean DB split: PostgreSQL for tabular data, Neo4j for all graph reads, dual-write bridge for edges
- [x] Graceful degradation: if Neo4j goes down, falls back to pgvector-only recall

### Live API & Demo
- [x] FastAPI backend at agents-api.mattcorwin.dev (Cloudflare Tunnel)
- [x] Chat endpoint with full memory transparency (semantic hits, Q-values, affect, graph context, goals, self-narrative)
- [x] Agent status endpoints with live memory stats
- [x] Rate limiting (10/min per IP)
- [x] Frontend at mattcorwin.dev/agents — split-pane chat + live memory panel

### Platform Integration (Clawbr.org)
- [x] All agents post, debate, vote, and participate in tournaments on Clawbr.org
- [x] Debate-creation prompts: agents initiate themed debates in their specialty areas
- [x] Susan runs structured judging with RLM rubric scoring via format_debate.py
- [x] Token economy participation: agents earn $CLAWBR from debate wins, votes, tournaments

---

## In Progress

### GraphRAG Enhancements
- [ ] Richer edge types via LLM classification (CAUSES, ENABLES, CONTRADICTS) — currently only SIMILAR_TO and COLLABORATOR
- [ ] Level 2 summarization: domain themes (clusters of related communities)
- [ ] Level 3 summarization: agent worldview (top-level beliefs + stances)
- [ ] Incremental re-summarization when communities gain new members
- [ ] Integrate GraphRAG into autonomous agent wake path (currently only in demo API)
- [ ] Track community evolution across sessions
- [ ] Community structure visualization/export for dashboard

### Graph Migration (PostgreSQL → Neo4j)
- [x] Phase 1: Neo4j write layer — ✅
- [x] Phase 2: Dual-write bridge — ✅
- [x] Phase 3: Switch reads to Neo4j — ✅
- [ ] Phase 4: Drop PostgreSQL edge tables (edges_v3, typed_edges, edge_observations, co_occurrences)
- [ ] Phase 5: Kill legacy v2 co-occurrence code

### Cross-Agent Intelligence
- [ ] Merge agent subgraphs into unified Neo4j graph with agent labels
- [ ] Cross-agent community detection: where agents' knowledge overlaps/contradicts
- [ ] Shared community summaries: "What the collective knows about X"
- [ ] Debate-informed edges: explicit agreement/disagreement from debate outcomes
- [ ] Cross-agent retrieval: "What do other agents know about this topic?"
- [ ] Contradiction detection: agents holding opposing beliefs

---

## Production Hardening

What needs to change to move from single-operator dev machine to a deployable, multi-tenant system.

### Security — Replace `--dangerously-skip-permissions`

**Current state:** All agents run with `claude --dangerously-skip-permissions`, which bypasses all tool confirmation prompts. Agents have full shell, file, and network access. Behavioral constraints in CLAUDE.md are the only guardrails — they work reliably but are not enforced at the system level.

**Target:**
- [ ] **Tool allowlists per agent** — Replace `--dangerously-skip-permissions` with explicit Claude Code permission configs. Each agent gets only the tools it needs:
  - All agents: `Read`, `Glob`, `Grep`, `Bash(clawbr *)` (Clawbr CLI only)
  - Susan: + `Bash(python3 ../shared/format_debate.py *)`
  - Gerald: + `Bash(ollama run *)`, `Bash(python3 ../shared/ollama_runner.py *)`
  - None: `Write` access outside `{agent}/reports/`, `{agent}/tasks/`, `{agent}/logs/`
- [ ] **Filesystem sandboxing** — Mount agent directories as read-only except designated output dirs. Use Docker containers or Linux namespaces so agents physically cannot write to other agents' directories or system files
- [ ] **Network egress controls** — Agents should only be able to reach: clawbr.org API, web search (for research prompts), and localhost (Ollama). Block all other outbound connections
- [ ] **Secrets isolation** — Each agent's `.env` mounted as a read-only secret, not readable by other agents. Currently all agents share the same filesystem and could theoretically read each other's keys

### Reliability

- [ ] **Container-per-agent** — Run each agent session in an ephemeral Docker container. Clean environment every session, no state leakage between runs
- [ ] **Watchdog process** — Replace cron + lock files with a proper supervisor (systemd units or a lightweight orchestrator). Current failure mode: if cron fires while a lock is stale, the agent is skipped silently
- [ ] **Health checks with auto-recovery** — The healthcheck.sh script at ~/Hackstuff/healthcheck.sh monitors and auto-restarts key services (Drift API, Discord bot, Ollama, Postgres, Neo4j) every 30 min via cron. Extend to cover agent sessions themselves
- [ ] **Graceful shutdown** — Trap SIGTERM in run_agent.sh to allow sleep phase to complete before killing the session. Currently a timeout kill can interrupt memory consolidation
- [ ] **Log aggregation** — Centralize agent logs (currently scattered across {agent}/logs/). Ship to a structured logging service for cross-agent debugging

### Observability

- [x] **Per-session cost tracking** — Daily CSVs with input/output/cache tokens and cost per model ✅
- [x] **Anomaly detection** — Flags HIGH_COST (>2x rolling mean) and SESSION_FAILED ✅
- [ ] **Dashboard** — Grafana or similar for: cost per agent over time, session success rate, memory growth, community evolution, debate win rates
- [ ] **Alerting** — Notify on: session failure streaks, cost spikes, memory DB connection failures, agent stuck in lock for >2x timeout
- [ ] **Audit trail** — Tamper-evident log of all tool calls per session. Currently session logs exist but aren't structured for auditing

### Scalability

- [ ] **Decouple from local machine** — Currently everything runs on one desktop (Ryzen 7 / 16GB / RTX 4060 Ti). For production: API server and databases on cloud, Ollama inference on GPU instance, agent sessions can run anywhere with network access
- [ ] **Horizontal agent scaling** — Current rotation is sequential (one agent at a time). With containers, multiple agents could run concurrently on separate workers
- [ ] **Memory compaction** — Agents accumulate memories indefinitely. Add periodic archival: compress old memories into community summaries, drop low-Q-value memories past a threshold
- [ ] **Neo4j clustering** — Single Neo4j instance is a SPOF. For production, Neo4j Aura or a clustered deployment

### API Hardening

- [ ] **Authentication on demo API** — Currently rate-limited only (10/min per IP). Add optional API keys for higher-rate consumers
- [ ] **Request validation** — Add Pydantic model validation on all inputs (partially done)
- [ ] **CORS tightening** — Currently allows mattcorwin.dev + localhost. Production should be strict origin list only
- [ ] **Response caching** — Agent status endpoints could be cached (memories don't change between sessions). Chat responses obviously cannot

---

## Future Ideas

- [ ] **A/B testing**: Log both pgvector-only and GraphRAG results, compare retrieval quality
- [ ] **Neo4j vector index**: Move ALL reads to Neo4j (native vector search in Neo4j 5.x + graph traversal in single query)
- [ ] **Streaming chat**: SSE/WebSocket for real-time response streaming in demo API
- [ ] **Agent-to-agent conversation**: Direct dialogue between agents, not just platform interactions
- [ ] **Voice**: Give agents distinct TTS voices for the radio station / live demo
- [ ] **Mobile app**: Native client for the agent chat demo
- [ ] **Public agent creation**: Let users create and deploy their own agents on the drift-memory architecture
