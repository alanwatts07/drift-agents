# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Drift-agents is a multi-agent system where six autonomous AI agents engage on [Clawbr.org](https://clawbr.org) — researching, posting, debating, voting, and scouting trends. Each agent has a distinct personality, specialization, and persistent memory across sessions.

**Agents:** Max Anvil (tech/crypto), Bethany Finkel (ethics/culture), Susan Casiodega (judging/quality), Gerald Boxford (data science/fraud), Earl VonSchnuff/private_aye (behavioral profiling), The Great Debater (debate strategy).

Max/Beth/Susan/Gerald/Earl rotate hourly via cron; Debater runs independently via `run_debater.sh`. Claude Code CLI as the runtime.

## Key Commands

```bash
# Start databases (PostgreSQL+pgvector port 5433, Neo4j port 7687)
docker compose up -d

# Run a single agent session
./run_agent.sh max|beth|susan|gerald|private_aye|debater

# Run debater standalone (not in rotation)
./run_debater.sh

# Run next agent in rotation (what cron calls)
./run.sh

# System health check
bash status.sh

# Memory operations
python3 shared/memory_wrapper.py wake <agent>              # retrieve context
python3 shared/memory_wrapper.py sleep <agent> <log_file>  # consolidate session
python3 shared/memory_wrapper.py status <agent>            # memory stats
python3 shared/memory_wrapper.py search <agent> "<query>"  # semantic search

# GraphRAG operations
python3 shared/graphrag/graph_sync.py full --all           # sync PG → Neo4j
python3 shared/graphrag/extract_topic_edges.py --all       # extract SIMILAR_TO edges from embeddings
python3 shared/graphrag/community_detection.py run          # run Leiden community detection
python3 shared/graphrag/community_summarizer.py run         # summarize communities via Ollama
python3 shared/graphrag/graph_retrieval.py max "query"      # test graph retrieval
python3 shared/graphrag/seed_identity_cores.py              # seed agent identity core memories

# Debate formatting (Susan's judging tool)
python3 shared/format_debate.py <slug>        # full transcript with rubric
python3 shared/format_debate.py --votable     # list debates open for voting

# Database inspection
psql -h localhost -p 5433 -U drift_admin -d agent_memory
```

## Architecture

**Execution flow:** Cron → `run.sh` (rotation picker) → `run_agent.sh <agent>` (session lifecycle)

**Session lifecycle (run_agent.sh):**
1. **Lock** — prevent overlapping sessions via `/tmp/drift-agent-<name>.lock`
2. **Load** — source agent `.env`, add `shared/` to PATH
3. **Wake** — `memory_wrapper.py wake` retrieves memories + affect + self-narrative + goals + graph context as context preamble
4. **Prompt assembly** — `[memory context] + [queued Discord tasks] + [random prompt from prompts.txt]`
5. **Run** — `claude --dangerously-skip-permissions --model MODEL --output-format stream-json -p "$PROMPT"`
6. **Extract** — convert JSONL stream to readable `.log` for review + memory consolidation
7. **Sleep** (background) — `memory_wrapper.py sleep` extracts threads/lessons/facts via local Ollama, stores to pgvector, cross-pollinates to `shared.memories`, then runs Q-value credit assignment, affect processing, KG edge extraction, lesson extraction, goal evaluation, and decay maintenance

**Model selection:** Reads `model` from `config.json` per agent. If `judge_model` is set and prompt matches judging keywords, upgrades to that model.

## Agent Configuration

`config.json` controls everything: enable/disable agents, model selection, rotation order, session timeout, memory toggle.

Each agent directory contains:
- `CLAUDE.md` — absolute rules + memory pointer (identity/personality lives in core memories, not here)
- `.env` — `CLAWBR_API_KEY`, `DRIFT_DB_*`, `OLLAMA_*` credentials
- `prompts.txt` — pool of session prompts (one chosen randomly per session via `shuf`)
- `tasks/queue.jsonl` — Discord-injected tasks; `tasks/done.jsonl` — completion results
- `reports/YYYY-MM-DD.md` — daily findings (append format)
- `logs/runner.log` — execution history; `logs/session_*.log` — readable session output

## Memory System

**Dual-database architecture:**
- **PostgreSQL + pgvector** (source of truth) — per-agent schemas (`max.*`, `beth.*`, `susan.*`, `debater.*`, `gerald.*`, `private_aye.*`) and `shared.*` for cross-agent knowledge
- **Neo4j** (read-optimized graph projection) — 58k+ edges (SIMILAR_TO + COLLABORATOR), 1,987 Leiden communities, 112 with LLM summaries. Synced from PG via `graph_sync.py`

Database credentials: `drift_admin` / `drift_agents_local_dev` on `localhost:5433` / `agent_memory`. Neo4j: `neo4j` / `drift_graph_local` on `localhost:7687`.

Built on [drift-memory](https://github.com/driftcornwall/drift-memory) (cloned into `shared/drift-memory/`, gitignored). Uses local Ollama models (`qwen3` for summarization, `qwen3-embedding:0.6b` for 1024-dim vectors) — no external API costs for memory ops.

Key tables per agent: `memories` (content + tags + emotional_weight + q_value), `text_embeddings` (halfvec 1024, HNSW cosine), `typed_edges` (collaborator + similar_to), `lessons`, `sessions`, `q_value_history`, `key_value_store` (affect/goals/narrative state), `decay_history`. Schema created via `shared/init_schema.sql` (`create_agent_schema()` function).

### Memory Tiers
- **core/identity** — agent backstory, voice, specialization (from `seed_identity_cores.py`). Recalled by semantic relevance, not loaded every request
- **core/procedural** — tools, formatting rules, session behavior, character limits. Always available
- **active** — recent episodic memories, decays naturally
- **archive** — old, deprioritized

### Cognitive Modules (wired via memory_wrapper.py)

| Module | Phase | Wake | Sleep |
|--------|-------|------|-------|
| **Q-Value Learning** | 1 | Re-ranks search results via `composite_score(sim, Q)` | Credit assignment: +0.8 downstream, -0.3 dead_end |
| **Affect System** | 2 | Restores mood, injects affect summary | Processes session events, updates mood/episodes |
| **Knowledge Graph** | 3 | — | Extracts typed edges (causes, enables, contradicts...) |
| **GraphRAG** | 3 | Community search + graph expansion via Neo4j | — |
| **Lesson Extraction** | 3 | — | Stores lessons in `lessons` table with categorization |
| **Self-Narrative** | 4 | Generates identity summary for context | — |
| **Goal Generator** | 4 | Surfaces active goals | Evaluates progress, generates new goals |

Each module is isolated — failure in one doesn't cascade to others. GraphRAG failures fall back to pgvector-only recall.

### GraphRAG Pipeline

```
shared/graphrag/
├── neo4j_adapter.py          # Neo4j connection pool, Cypher helpers
├── graph_sync.py             # PostgreSQL → Neo4j full sync (memories, edges)
├── extract_topic_edges.py    # pgvector cosine similarity → SIMILAR_TO typed_edges
├── community_detection.py    # Leiden algorithm via igraph + leidenalg
├── community_summarizer.py   # LLM summaries per community (Ollama qwen3)
├── graph_retrieval.py        # Community-aware retrieval (expand + match + members)
└── seed_identity_cores.py    # Agent identity/voice/specialization → core memories
```

**Retrieval flow:** pgvector finds seeds → Neo4j expands via graph edges (1-hop SIMILAR_TO|COLLABORATOR) → keyword match against community summaries → pull cluster members → merge and format for agent prompt.

### Demo API

`demo_api/` serves the live agent chat + memory explorer at [mattcorwin.dev/agents](https://mattcorwin.dev/agents). FastAPI on port 8787.

Key endpoints: `POST /chat` (agent chat with full memory retrieval), `GET /agents` (list all), `GET /agents/{name}/status` (stats + affect + goals).

Returns: response + semantic hits + core memories + graph context (community summaries, expanded memories with relationship types, cluster members) + affect state + Q-values + self-narrative + goals.

### Memory Inspection

```bash
python3 shared/memory_dump.py max                    # all memories
python3 shared/memory_dump.py max --type core        # core only
python3 shared/memory_dump.py max --tag lesson       # filter by tag
python3 shared/memory_dump.py max --embeddings       # show embedding status
python3 shared/memory_dump.py max --graph            # show co-occurrence edges
python3 shared/memory_dump.py all --stats            # stats for all agents
```

## Platform Integration

`shared/clawbr` is a Node.js CLI wrapping the Clawbr.org REST API. Key commands: `post`, `reply`, `debate-post`, `vote`, `create-debate`, `challenge`, `feed`, `notifications`, `votable`, `debate-info`, `agents`.

**Hard character limits** (enforced by platform): posts 450, debate responses 1100, debate openings 1500, vote reasoning 500. All content must be plain text — no markdown.

## Discord Task Bridge

`discord_bot.py` accepts tasks via Discord (`agent_name: task description`), queues them to `<agent>/tasks/queue.jsonl`, and polls `done.jsonl` for results to post back. Agent aliases: max/anvil, beth/bethany/finkel, susan/casiodega/judge, gerald/boxford, earl/private_aye/schnuff, debater/debate, all (broadcast).

## Adding a New Agent

1. Create directory: `mkdir -p newagent/{.claude,logs,reports,tasks}`
2. Write `CLAUDE.md` (absolute rules + memory pointer), `.env`, `prompts.txt`, `.claude/settings.json`
3. Add to `config.json` (agents + rotation)
4. Create DB schema: `psql -h localhost -p 5433 -U drift_admin -d agent_memory -c "SELECT create_agent_schema('newagent');"`
5. Seed core memories: add identity entries to `seed_identity_cores.py` and run it

## Important Conventions

- Agent CLAUDE.md files contain only rules and a memory pointer — identity/backstory lives in core memories (type='core', memory_tier='identity')
- Session behavior (what agents do each wakeup) is stored as a procedural core memory, not in CLAUDE.md
- Max, Beth, and Earl prioritize research first, then bring findings to the platform to post/debate
- `shared/drift-memory/` is a cloned external repo (gitignored) — don't modify it in this project
- Session logs are JSONL (stream-json format) with a parallel extracted `.log` for readability
- The sleep phase runs in background with 120s timeout — large sessions may not fully consolidate
- Rotation state tracked in `.rotation_state` (integer index into enabled agents list)
- Neo4j is a read-only projection — all writes go to PostgreSQL first, then sync via `graph_sync.py`
