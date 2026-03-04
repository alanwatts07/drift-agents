#!/bin/bash
# Usage: run_agent.sh <agent_name>
AGENT="$1"
BASE="$HOME/Hackstuff/drift-agents"
DIR="$BASE/$AGENT"
LOCK="/tmp/drift-agent-${AGENT}.lock"
TIMEOUT=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['session_timeout_sec'])" 2>/dev/null || echo 600)

# Skip if disabled
ENABLED=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['agents']['$AGENT']['enabled'])" 2>/dev/null || echo "true")
[ "$ENABLED" = "False" ] && exit 0

# Prevent overlapping sessions
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then exit 0; fi
echo $$ > "$LOCK"; trap "rm -f $LOCK" EXIT

set -a; source "$DIR/.env" 2>/dev/null; set +a
export PATH="$BASE/shared:$PATH"

# Ensure Neo4j is running (GraphRAG)
if ! docker inspect drift-agents-graph --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
    docker compose -f "$BASE/docker-compose.yml" up -d neo4j 2>/dev/null
    sleep 5  # Give Neo4j a moment to accept connections
fi

# Read model preference from config (default: sonnet)
MODEL=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['agents']['$AGENT'].get('model', 'sonnet'))" 2>/dev/null || echo "sonnet")

# Check for judge_model override (used when prompt is judging-focused)
JUDGE_MODEL=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['agents']['$AGENT'].get('judge_model', ''))" 2>/dev/null || echo "")

# ── PROMPT: Select + check tasks (BEFORE wake for cue-based retrieval) ──
TASKS_FILE="$DIR/tasks/queue.jsonl"
PROMPT=$(shuf -n1 "$DIR/prompts.txt")
TASK_INJECT=""

# Upgrade to judge_model if prompt is judging-focused and judge_model is configured
if [ -n "$JUDGE_MODEL" ] && echo "$PROMPT" | grep -qiE 'votable|rubric|judging|format_debate'; then
    MODEL="$JUDGE_MODEL"
    echo "$(date -Iseconds) [model] Using judge_model=$MODEL for judging prompt" >> "$DIR/logs/runner.log"
fi

if [ -f "$TASKS_FILE" ] && [ -s "$TASKS_FILE" ]; then
    TASK_INJECT=$(python3 -c "
import json
tasks = []
for line in open('$TASKS_FILE'):
    line = line.strip()
    if line:
        try:
            t = json.loads(line)
            tasks.append(t)
        except: pass
if tasks:
    print('QUEUED TASKS — process these FIRST before your regular session:')
    for t in tasks:
        print(f\"  [{t['id']}] from {t['from']}: {t['task']}\")
    print()
    print('Write results to tasks/done.jsonl as described in your CLAUDE.md.')
    print()
" 2>/dev/null)

    if [ -n "$TASK_INJECT" ]; then
        PROMPT="${TASK_INJECT}Then continue with: ${PROMPT}"
    fi
    # Archive queue so tasks aren't re-processed
    mv "$TASKS_FILE" "$DIR/tasks/queue.processed.$(date +%s)"
fi

# ── CONTEXT + CUE-BASED WAKE ──
MEMORY_CONTEXT=""
CONTEXT_SUMMARY=""
MEMORY_ENABLED=$(python3 -c "import json; c=json.load(open('$BASE/config.json')); print(c.get('memory_enabled', True))" 2>/dev/null || echo "true")

if [ "$MEMORY_ENABLED" = "True" ] || [ "$MEMORY_ENABLED" = "true" ]; then
    # Phase 1: Context gather + plan
    PLAN_JSON=$(timeout 8 python3 "$BASE/shared/context_gather.py" "$AGENT" "$PROMPT" "$TASK_INJECT" 2>/dev/null) || PLAN_JSON=""
    PLAN_TEXT=""
    if [ -n "$PLAN_JSON" ]; then
        PLAN_TEXT=$(echo "$PLAN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('plan',''))" 2>/dev/null) || PLAN_TEXT=""
        CONTEXT_SUMMARY=$(echo "$PLAN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('context_summary',''))" 2>/dev/null) || CONTEXT_SUMMARY=""
        ELAPSED=$(echo "$PLAN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('elapsed_ms',0))" 2>/dev/null) || ELAPSED=0
        echo "$(date -Iseconds) [context] Plan: '${PLAN_TEXT:0:80}' (${ELAPSED}ms)" >> "$DIR/logs/runner.log"
    fi

    # Phase 2: Cue-based memory retrieval
    if [ -n "$PLAN_TEXT" ]; then
        CUE_FILE="/tmp/drift-cue-${AGENT}.txt"
        printf '%s' "$PLAN_TEXT" > "$CUE_FILE"
        MEMORY_CONTEXT=$(timeout 7 python3 "$BASE/shared/memory_wrapper.py" wake_cue "$AGENT" "@${CUE_FILE}" 2>/dev/null) || MEMORY_CONTEXT=""
        rm -f "$CUE_FILE"
    fi

    # Fallback: standard wake if cue-based failed
    if [ -z "$MEMORY_CONTEXT" ]; then
        MEMORY_CONTEXT=$(timeout 5 python3 "$BASE/shared/memory_wrapper.py" wake "$AGENT" 2>/dev/null) || MEMORY_CONTEXT=""
    fi

    if [ -n "$MEMORY_CONTEXT" ]; then
        echo "$(date -Iseconds) [wake] Retrieved memory context (${#MEMORY_CONTEXT} chars)" >> "$DIR/logs/runner.log"
    fi
fi

# ── BUILD: [memory] + [platform context] + [prompt] ──
if [ -n "$CONTEXT_SUMMARY" ]; then
    PROMPT="=== PLATFORM STATE (live) ===
${CONTEXT_SUMMARY}
===

${PROMPT}"
fi

if [ -n "$MEMORY_CONTEXT" ]; then
    PROMPT="${MEMORY_CONTEXT}

${PROMPT}"
fi

TS=$(date +%Y%m%d_%H%M%S)
SESSION_LOG="$DIR/logs/session_${TS}.log"
PROMPT_FILE="/tmp/drift-agent-${AGENT}-prompt.txt"

# Write prompt to temp file to avoid shell argument limits / TTY issues
printf '%s' "$PROMPT" > "$PROMPT_FILE"

# ── RUN: Execute agent session ──
cd "$DIR"

if [[ "$MODEL" == ollama:* ]]; then
    OLLAMA_MODEL="${MODEL#ollama:}"
    EXECUTOR=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['agents']['$AGENT'].get('executor', ''))" 2>/dev/null || echo "")

    if [[ -n "$EXECUTOR" ]]; then
        # HYBRID MODE: Qwen thinks → Claude executes
        echo "$(date -Iseconds) [hybrid] Phase 1: thinking with $OLLAMA_MODEL" >> "$DIR/logs/runner.log"
        THINK_LOG="$DIR/logs/think_${TS}.log"
        timeout 120 python3 "$BASE/shared/ollama_runner.py" \
            "$DIR" "$PROMPT_FILE" "$OLLAMA_MODEL" \
            --think-only --timeout 120 \
            > "$THINK_LOG" 2>> "$DIR/logs/runner.log"

        if [ -s "$THINK_LOG" ]; then
            echo "$(date -Iseconds) [hybrid] Phase 2: executing with $EXECUTOR" >> "$DIR/logs/runner.log"
            EXEC_PROMPT="/tmp/drift-exec-${AGENT}.txt"
            cat > "$EXEC_PROMPT" << 'EXECEOF'
You are executing actions for a drift-agent. Below is the agent's session plan, generated by a local Qwen model. The plan describes what the agent wants to do, including shell commands and post content.

YOUR JOB:
1. Read the plan and understand each intended action
2. Execute each action using bash (clawbr CLI is on PATH)
3. PRESERVE the agent's EXACT words, spelling, and voice in ALL posts, replies, debate arguments, and votes — do NOT rewrite, improve, or clean up their content
4. If the intended command syntax is slightly wrong, fix the syntax but keep the content word-for-word
5. After executing, briefly summarize what happened as the agent would

AGENT'S SESSION PLAN:
---
EXECEOF
            cat "$THINK_LOG" >> "$EXEC_PROMPT"
            printf '\n---\nExecute now. Stay in character as the agent.\n' >> "$EXEC_PROMPT"

            STREAM_LOG="${SESSION_LOG%.log}.jsonl"
            unset CLAUDECODE
            timeout "$TIMEOUT" claude --dangerously-skip-permissions --model "$EXECUTOR" \
                --output-format stream-json --verbose \
                -p "$(cat "$EXEC_PROMPT")" \
                < /dev/null > "$STREAM_LOG" 2>&1
            EXIT_CODE=$?
            rm -f "$EXEC_PROMPT"

            # Extract readable text (same as Claude path)
            python3 -c "
import json, sys
for line in open('$STREAM_LOG'):
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        if obj.get('type') == 'assistant' and 'message' in obj:
            for block in obj['message'].get('content', []):
                if block.get('type') == 'text':
                    print(block['text'])
        elif obj.get('type') == 'result' and 'result' in obj:
            print(obj['result'])
    except: pass
" > "$SESSION_LOG" 2>/dev/null
        else
            echo "$(date -Iseconds) [hybrid] Phase 1 produced no output" >> "$DIR/logs/runner.log"
            EXIT_CODE=1
        fi
    else
        # Pure Ollama mode (direct tool calling — for models that support it)
        timeout "$TIMEOUT" python3 "$BASE/shared/ollama_runner.py" \
            "$DIR" "$PROMPT_FILE" "$OLLAMA_MODEL" \
            --max-turns 15 --timeout "$TIMEOUT" \
            > "$SESSION_LOG" 2>> "$DIR/logs/runner.log"
        EXIT_CODE=$?
    fi
else
    # Claude model — existing path
    STREAM_LOG="${SESSION_LOG%.log}.jsonl"
    timeout "$TIMEOUT" claude --dangerously-skip-permissions --model "$MODEL" \
      --output-format stream-json --verbose \
      -p "$(cat "$PROMPT_FILE")" \
      < /dev/null > "$STREAM_LOG" 2>&1

    EXIT_CODE=$?

    # Extract readable text from stream-json for human review + memory consolidation
    python3 -c "
import json, sys
for line in open('$STREAM_LOG'):
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        if obj.get('type') == 'assistant' and 'message' in obj:
            for block in obj['message'].get('content', []):
                if block.get('type') == 'text':
                    print(block['text'])
        elif obj.get('type') == 'result' and 'result' in obj:
            print(obj['result'])
    except: pass
" > "$SESSION_LOG" 2>/dev/null
fi

rm -f "$PROMPT_FILE"

echo "$(date -Iseconds) exit=$EXIT_CODE prompt='${PROMPT:0:60}...'" >> "$DIR/logs/runner.log"

# ── SLEEP: Consolidate session into memories (background) ──
if [ "$MEMORY_ENABLED" = "True" ] || [ "$MEMORY_ENABLED" = "true" ]; then
    if [ -f "$SESSION_LOG" ] && [ -s "$SESSION_LOG" ]; then
        (
            timeout 120 python3 "$BASE/shared/memory_wrapper.py" sleep "$AGENT" "$SESSION_LOG" \
                >> "$DIR/logs/runner.log" 2>&1
        ) &
        echo "$(date -Iseconds) [sleep] Memory consolidation started (pid $!)" >> "$DIR/logs/runner.log"
    fi
fi
