#!/bin/bash
# Interactive conversation with a drift-agent using cue-based memory.
# Usage: ./chat.sh <agent> "Your message here"
#
# The user's message IS the retrieval cue — no planning step needed.
# No sleep phase — chat conversations aren't consolidated.

AGENT="$1"
MESSAGE="$2"
BASE="$HOME/Hackstuff/drift-agents"
DIR="$BASE/$AGENT"

if [ -z "$AGENT" ] || [ -z "$MESSAGE" ]; then
    echo "Usage: ./chat.sh <agent> \"Your message\""
    echo "Agents: max, beth, susan, debater, gerald"
    exit 1
fi

if [ ! -d "$DIR" ]; then
    echo "Unknown agent: $AGENT"
    exit 1
fi

# Source agent's env for DB + Ollama credentials
set -a; source "$DIR/.env" 2>/dev/null; set +a
export PATH="$BASE/shared:$PATH"

# Cue-based memory retrieval — user's question is the cue
CUE_FILE="/tmp/drift-chat-cue-${AGENT}.txt"
printf '%s' "$MESSAGE" > "$CUE_FILE"
MEMORY=$(timeout 7 python3 "$BASE/shared/memory_wrapper.py" wake_cue "$AGENT" "@${CUE_FILE}" 2>/dev/null) || MEMORY=""
rm -f "$CUE_FILE"

# Fallback to standard wake
if [ -z "$MEMORY" ]; then
    MEMORY=$(timeout 5 python3 "$BASE/shared/memory_wrapper.py" wake "$AGENT" 2>/dev/null) || MEMORY=""
fi

# Read CLAUDE.md for personality (first 80 lines — identity + rules)
IDENTITY=""
if [ -f "$DIR/CLAUDE.md" ]; then
    IDENTITY=$(head -80 "$DIR/CLAUDE.md")
fi

# Build prompt
PROMPT_FILE="/tmp/drift-chat-${AGENT}-prompt.txt"
{
    if [ -n "$IDENTITY" ]; then
        echo "$IDENTITY"
        echo ""
    fi
    if [ -n "$MEMORY" ]; then
        echo "$MEMORY"
        echo ""
    fi
    echo "Operator: $MESSAGE"
} > "$PROMPT_FILE"

# Read model from config
MODEL=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['agents']['$AGENT'].get('model', 'sonnet'))" 2>/dev/null || echo "sonnet")

if [[ "$MODEL" == ollama:* ]]; then
    # Ollama model — use ollama_runner with no tools (pure conversation)
    OLLAMA_MODEL="${MODEL#ollama:}"
    python3 "$BASE/shared/ollama_runner.py" \
        "$DIR" "$PROMPT_FILE" "$OLLAMA_MODEL" \
        --max-turns 1 --timeout 120 \
        2>/dev/null
else
    # Claude model
    claude --model "$MODEL" -p "$(cat "$PROMPT_FILE")" < /dev/null 2>/dev/null
fi

rm -f "$PROMPT_FILE"
