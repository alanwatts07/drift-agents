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

# Read model preference from config (default: sonnet)
MODEL=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['agents']['$AGENT'].get('model', 'sonnet'))" 2>/dev/null || echo "sonnet")

# Check for judge_model override (used when prompt is judging-focused)
JUDGE_MODEL=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['agents']['$AGENT'].get('judge_model', ''))" 2>/dev/null || echo "")

# ── WAKE: Retrieve memories ──
MEMORY_CONTEXT=""
MEMORY_ENABLED=$(python3 -c "import json; c=json.load(open('$BASE/config.json')); print(c.get('memory_enabled', True))" 2>/dev/null || echo "true")

if [ "$MEMORY_ENABLED" = "True" ] || [ "$MEMORY_ENABLED" = "true" ]; then
    MEMORY_CONTEXT=$(timeout 5 python3 "$BASE/shared/memory_wrapper.py" wake "$AGENT" 2>/dev/null) || MEMORY_CONTEXT=""
    if [ -n "$MEMORY_CONTEXT" ]; then
        echo "$(date -Iseconds) [wake] Retrieved memory context (${#MEMORY_CONTEXT} chars)" >> "$DIR/logs/runner.log"
    fi
fi

# Check for queued tasks from Discord
TASKS_FILE="$DIR/tasks/queue.jsonl"
PROMPT=$(shuf -n1 "$DIR/prompts.txt")

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

# Build final prompt: [memory context] + [tasks] + [random prompt]
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
STREAM_LOG="${SESSION_LOG%.log}.jsonl"
timeout "$TIMEOUT" claude --dangerously-skip-permissions --model "$MODEL" \
  --output-format stream-json --verbose \
  -p "$(cat "$PROMPT_FILE")" \
  < /dev/null > "$STREAM_LOG" 2>&1

EXIT_CODE=$?
rm -f "$PROMPT_FILE"

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
