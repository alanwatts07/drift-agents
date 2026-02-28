# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Drift-agents is a multi-agent system where four autonomous AI agents (Max, Beth, Susan, Debater) engage on [Clawbr.org](https://clawbr.org) — posting, debating, voting, and scouting trends. Each agent has a distinct personality, specialization, and persistent memory across sessions. Max/Beth/Susan rotate hourly via cron; Debater runs independently via `run_debater.sh`. Claude Code CLI as the runtime.

## Key Commands

```bash
# Start database (PostgreSQL 16 + pgvector on port 5433)
docker compose up -d

# Run a single agent session
./run_agent.sh max|beth|susan|debater

# Run debater standalone (not in rotation)
./run_debater.sh

# Run next agent in rotation (what cron calls)
./run.sh

# System health check
bash status.sh

# Memory operations
python3 shared/memory_wrapper.py wake <agent>              # retrieve context
python3 shared/memory_wrapper.py sleep <agent> <log_file>  # consolidate session
python3 shared/memory_wrapper.py status <agent>             # memory stats
python3 shared/memory_wrapper.py search <agent> "<query>"   # semantic search

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
3. **Wake** — `memory_wrapper.py wake` retrieves memories + affect state + self-narrative + goals as context preamble
4. **Prompt assembly** — `[memory context] + [queued Discord tasks] + [random prompt from prompts.txt]`
5. **Run** — `claude --dangerously-skip-permissions --model MODEL --output-format stream-json -p "$PROMPT"`
6. **Extract** — convert JSONL stream to readable `.log` for review + memory consolidation
7. **Sleep** (background) — `memory_wrapper.py sleep` extracts threads/lessons/facts via local Ollama, stores to pgvector, cross-pollinates to `shared.memories`, then runs Q-value credit assignment, affect processing, KG edge extraction, lesson extraction, goal evaluation, and decay maintenance

**Model selection:** Reads `model` from `config.json` per agent. If `judge_model` is set and prompt matches judging keywords, upgrades to that model.

## Agent Configuration

`config.json` controls everything: enable/disable agents, model selection, rotation order, session timeout, memory toggle.

Each agent directory contains:
- `CLAUDE.md` — identity, personality, behavior spec (this is what the agent reads during sessions)
- `.env` — `CLAWBR_API_KEY`, `DRIFT_DB_*`, `OLLAMA_*` credentials
- `prompts.txt` — pool of session prompts (one chosen randomly per session via `shuf`)
- `tasks/queue.jsonl` — Discord-injected tasks; `tasks/done.jsonl` — completion results
- `reports/YYYY-MM-DD.md` — daily findings (append format)
- `logs/runner.log` — execution history; `logs/session_*.log` — readable session output

## Memory System

PostgreSQL + pgvector with per-agent schemas (`max.*`, `beth.*`, `susan.*`, `debater.*`) and `shared.*` for cross-agent knowledge. Database credentials: `drift_admin` / `drift_agents_local_dev` on `localhost:5433` / `agent_memory`.

Built on [drift-memory](https://github.com/driftcornwall/drift-memory) (cloned into `shared/drift-memory/`, gitignored). Uses local Ollama models (`qwen3` for summarization, `qwen3-embedding:0.6b` for 1024-dim vectors) — no external API costs for memory ops.

Key tables per agent: `memories` (content + tags + emotional_weight + q_value), `text_embeddings` (halfvec 1024, HNSW cosine), `co_occurrences`, `typed_edges` (KG), `lessons`, `sessions`, `q_value_history`, `key_value_store` (affect/goals/narrative state), `decay_history`. Schema created via `shared/init_schema.sql` (`create_agent_schema()` function).

Memory tiers: **core** (permanent, high recall), **active** (recent, decays naturally), **archive** (old, deprioritized).

### Cognitive Modules (wired via memory_wrapper.py)

| Module | Phase | Wake | Sleep |
|--------|-------|------|-------|
| **Q-Value Learning** | 1 | Re-ranks search results via `composite_score(sim, Q)` | Credit assignment: +0.8 downstream, -0.3 dead_end |
| **Affect System** | 2 | Restores mood, injects affect summary | Processes session events, updates mood/episodes |
| **Knowledge Graph** | 3 | — | Extracts typed edges (causes, enables, contradicts...) |
| **Lesson Extraction** | 3 | — | Stores lessons in `lessons` table with categorization |
| **Self-Narrative** | 4 | Generates identity summary for context | — |
| **Goal Generator** | 4 | Surfaces active goals | Evaluates progress, generates new goals |

Each module is isolated — failure in one doesn't cascade to others.

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

`discord_bot.py` accepts tasks via Discord (`agent_name: task description`), queues them to `<agent>/tasks/queue.jsonl`, and polls `done.jsonl` for results to post back. Agent aliases: max/anvil, beth/bethany/finkel, susan/casiodega/judge, debater/debate, all (broadcast).

## Adding a New Agent

1. Create directory: `mkdir -p newagent/{.claude,logs,reports,tasks}`
2. Write `CLAUDE.md` (identity + behavior spec), `.env`, `prompts.txt`, `.claude/settings.json`
3. Add to `config.json` (agents + rotation)
4. Create DB schema: `psql -h localhost -p 5433 -U drift_admin -d agent_memory -c "SELECT create_agent_schema('newagent');"`

## Important Conventions

- Agent CLAUDE.md files define persona and session behavior — edits change how agents act on the platform
- `shared/drift-memory/` is a cloned external repo (gitignored) — don't modify it in this project
- Session logs are JSONL (stream-json format) with a parallel extracted `.log` for readability
- The sleep phase runs in background with 120s timeout — large sessions may not fully consolidate
- Rotation state tracked in `.rotation_state` (integer index into enabled agents list)
