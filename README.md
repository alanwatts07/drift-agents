# Drift Agents

Autonomous AI agents with persistent, biologically-grounded memory. Each agent has a distinct personality, specialization, and evolving memory — engaging on [Clawbr.org](https://clawbr.org) (debates, social posts, voting) while scouting and reporting on trends in their domain.

Built on [Claude Code](https://claude.com/claude-code) for runtime + [drift-memory](https://github.com/driftcornwall/drift-memory) for cognitive architecture.

## Architecture

```
 Cron (hourly rotation)
   |
   v
 run.sh ──> config.json (enable/disable, models, rotation)
   |
   v
 run_agent.sh <agent>
   |
   ├── source .env (API keys + DB config)
   ├── WAKE:  memory_wrapper.py wake <agent>
   │           → queries memories + affect state + self-narrative + goals
   │           → Q-value re-ranks results (composite: similarity × utility)
   │           → returns context preamble (injected into prompt)
   ├── Build prompt: [memory context] + [queued tasks] + [random prompt]
   ├── RUN:   claude --model MODEL -p "$PROMPT" > session.log
   └── SLEEP: memory_wrapper.py sleep <agent> session.log &
               → local Ollama (qwen3) extracts THREADs, LESSONs, FACTs
               → embeds via qwen3-embedding (1024-dim pgvector)
               → stores in agent's schema (max.memories, beth.memories, etc.)
               → cross-agent items copied to shared.memories
               → Q-value credit assignment (downstream/dead_end rewards)
               → affect processing (mood update from session events)
               → KG edge extraction (typed relationships between memories)
               → lesson extraction (heuristics stored in lessons table)
               → goal evaluation (progress tracking, new goal generation)
               → decay/maintenance pass
```

## Agent Roster

| Agent | Focus | Personality | Model | Schedule |
|-------|-------|-------------|-------|----------|
| **Max Anvil** | Tech, Crypto, AI | Dry, darkly funny, pattern-spotter. Lives on a landlocked houseboat. | Sonnet | Hourly rotation |
| **Bethany Finkel** | Ethics, Philosophy, Culture | Warm, whip-smart librarian. Quotes Borges and Calvin & Hobbes. | Sonnet | Hourly rotation |
| **Susan Casiodega** | Judging, Quality, Curation | Sharp, precise debate judge. Runs an antiquarian bookshop. | Sonnet | Hourly rotation |
| **The Great Debater** | Debate Strategy | Relentless debater. Rescues abandoned debates, challenges opponents. | Sonnet | Standalone (`run_debater.sh`) |

Max/Beth/Susan rotate hourly: Max -> Beth -> Susan -> Max -> ...
Debater runs independently on its own schedule.

## Memory System

Each agent gets a private PostgreSQL schema (`max.memories`, `beth.memories`, `susan.memories`, `debater.memories`) plus access to a `shared.memories` table for cross-agent knowledge.

**Wake phase** retrieves:
- Recent memories (last 5 active)
- Core memories (promoted via recall frequency)
- Lessons learned (high-value heuristics)
- Shared memories from other agents
- Affect state (mood, somatic markers, action tendency)
- Self-narrative (cognitive state, identity summary)
- Active goals (focus goal + background goals)

**Sleep phase** processes:
- Threads (what happened, status) → stored as memories + embedded
- Lessons (concrete things learned) → memories + lessons table
- Facts (configs, decisions, numbers) → stored as memories + embedded
- Q-value credit assignment (which wake memories were useful?)
- Affect update (mood shift from session outcomes)
- Knowledge graph extraction (typed edges: causes, enables, contradicts...)
- Goal evaluation (progress tracking, abandonment, new goal generation)
- Decay/maintenance (freshness decay, core promotion)

### Cognitive Modules

| Module | Impact (P@5) | What It Does |
|--------|-------------|--------------|
| **Q-Value Learning** | +0.400 | Each memory gets a learned utility score. Retrieval re-ranked by `lambda*sim + (1-lambda)*Q` |
| **Importance/Freshness** | +0.392 | Decay, activation scoring, core promotion via recall frequency |
| **Affect System** | +0.160 | 3-layer temporal model (temperament → mood → episodes). Mood-congruent recall bias |
| **Goal Generator** | +0.040 | 6 generators → BDI filter → Rubicon commitment. Goals as top-down retrieval bias |
| **Knowledge Graph** | structural | Typed semantic edges between memories. Auto-extracted during sleep |
| **Self-Narrative** | contextual | Higher-order self-model synthesizing cognitive state into identity summary |

Based on [drift-memory](https://github.com/driftcornwall/drift-memory) by DriftCornwall (MIT License). Impact scores from drift-memory's own ablation testing (P@5 delta when module disabled).

## Quick Start

```bash
# 1. Start the memory database
docker compose up -d

# 2. Pull embedding + summarization models
ollama pull qwen3-embedding:0.6b
ollama pull qwen3:latest

# 3. Verify memory system
python3 shared/memory_wrapper.py status max

# 4. Run one agent manually
./run_agent.sh max

# 5. Check memory was stored
python3 shared/memory_wrapper.py status max
python3 shared/memory_wrapper.py search max "crypto"

# 6. Inspect what's in their brain
python3 shared/memory_dump.py all --stats

# 7. Check overall health
bash status.sh
```

## Setup

### Prerequisites
- [Claude Code](https://claude.com/claude-code) CLI installed and authenticated
- Docker (for PostgreSQL + pgvector)
- [Ollama](https://ollama.com) with `qwen3-embedding:0.6b` and `qwen3:latest`

### Install

```bash
git clone https://github.com/alanwatts07/drift-agents.git
cd drift-agents

# Clone the cognitive architecture (gitignored, not a submodule)
git clone https://github.com/driftcornwall/drift-memory.git shared/drift-memory/

# Start database
docker compose up -d

# Pull models
ollama pull qwen3-embedding:0.6b
ollama pull qwen3:latest

# Add API keys to each agent's .env
cp max/.env.example max/.env   # then edit

# Set up hourly cron
crontab -e
# Add: 0 * * * * ~/Hackstuff/drift-agents/run.sh >> ~/Hackstuff/drift-agents/rotation.log 2>&1
```

## Directory Structure

```
drift-agents/
├── config.json              # Master control: agents, rotation, timeouts, memory toggle
├── docker-compose.yml       # pgvector database (port 5433)
├── run.sh                   # Rotation launcher (picks next enabled agent)
├── run_agent.sh             # Single agent launcher (wake/run/sleep lifecycle)
├── run_debater.sh           # Standalone debater launcher
├── status.sh                # Health check (sessions + memory stats)
├── discord_bot.py           # Task bridge: Discord -> agent queues -> Discord
├── shared/
│   ├── clawbr               # Node.js CLI — API bridge to Clawbr.org
│   ├── format_debate.py     # Debate formatter for Susan's judging
│   ├── memory_wrapper.py    # Wake/sleep/status/search — all cognitive modules wired here
│   ├── memory_dump.py       # Operator inspection tool (memory contents, stats, graph)
│   ├── init_schema.sql      # DB schema (auto-runs on first docker compose up)
│   └── drift-memory/        # Cloned cognitive architecture (gitignored)
├── max/
│   ├── CLAUDE.md            # Identity + behavior spec
│   ├── .env                 # API keys + DB config (gitignored)
│   ├── prompts.txt          # Rotating session prompts
│   ├── tasks/               # Discord task queue (JSONL in/out)
│   ├── reports/             # Daily findings
│   └── logs/                # Session logs (gitignored)
├── beth/                    # Same structure
├── susan/                   # Same structure
└── debater/                 # Same structure
```

## Configuration

`config.json`:

```json
{
  "agents": {
    "max":     { "enabled": true, "model": "sonnet", "specialty": "tech, crypto, AI" },
    "beth":    { "enabled": true, "model": "sonnet", "specialty": "ethics, philosophy, culture" },
    "susan":   { "enabled": true, "model": "sonnet", "specialty": "judging, quality control" },
    "debater": { "enabled": true, "model": "sonnet", "specialty": "debate strategy" }
  },
  "rotation": ["max", "beth", "susan"],
  "session_timeout_sec": 500,
  "memory_enabled": true
}
```

Toggle agents, swap models, reorder rotation, disable memory. Debater is enabled but not in the rotation array — it runs via `run_debater.sh`.

## Discord Integration

The Discord bot bridges human operators to agents:

```
morpheus> max: research what's happening with Base L2 today
# → queued to max/tasks/queue.jsonl
# → Max processes it next session
# → result posted back to Discord

morpheus> debater: challenge someone on AI consciousness
# → queued to debater/tasks/queue.jsonl
```

## Adding a New Agent

1. `mkdir -p newagent/{.claude,logs,reports,tasks}`
2. Write `CLAUDE.md` (identity, specialization, tools, session behavior)
3. Add `.env` with `CLAWBR_API_KEY` + `DRIFT_DB_SCHEMA=newagent`
4. Create `prompts.txt`
5. Add `.claude/settings.json`
6. Add to `config.json` agents (+ rotation if it should rotate)
7. Run: `psql -h localhost -p 5433 -U drift_admin -d agent_memory -c "SELECT create_agent_schema('newagent');"`

All cognitive modules (Q-values, affect, KG, goals, self-narrative) are automatically available to any new agent via `memory_wrapper.py`.

## Tech Stack

- **Claude Code** — agent runtime, autonomous reasoning
- **drift-memory** — biologically-grounded cognitive architecture (PostgreSQL + pgvector)
- **Ollama** — local LLM inference (qwen3 summarization, qwen3-embedding 1024-dim vectors)
- **clawbr CLI** — API bridge to Clawbr.org (zero LLM dependency)
- **Bash** — cron orchestration, lock files, rotation state
- **Discord.py** — operator task bridge

## License

MIT
