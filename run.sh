#!/bin/bash
# Pick the next agent in rotation and run them
BASE="$HOME/Hackstuff/drift-agents"
STATE="$BASE/.rotation_state"

# Read rotation from config
AGENTS=($(python3 -c "
import json
c = json.load(open('$BASE/config.json'))
enabled = [a for a in c['rotation'] if c['agents'][a]['enabled']]
print(' '.join(enabled))
"))

# Get next index
IDX=0
[ -f "$STATE" ] && IDX=$(cat "$STATE")
IDX=$((IDX % ${#AGENTS[@]}))

AGENT="${AGENTS[$IDX]}"
echo "$((IDX + 1))" > "$STATE"

echo "$(date -Iseconds) Running $AGENT (slot $((IDX+1))/${#AGENTS[@]})"
"$BASE/run_agent.sh" "$AGENT"
