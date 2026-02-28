#!/bin/bash
BASE="$HOME/Hackstuff/drift-agents"
echo "=== Drift Agents Status ==="
echo ""
for agent in max beth susan; do
  ENABLED=$(python3 -c "import json; print(json.load(open('$BASE/config.json'))['agents']['$agent']['enabled'])" 2>/dev/null || echo "?")
  LATEST=$(ls -t "$BASE/$agent/logs/session_"*.log 2>/dev/null | head -1)
  COUNT=$(ls "$BASE/$agent/logs/session_"*.log 2>/dev/null | wc -l)
  ERRORS=$(grep -l "error\|Error\|FAILED" "$BASE/$agent/logs/session_"*.log 2>/dev/null | wc -l)
  REPORTS=$(ls "$BASE/$agent/reports/"*.md 2>/dev/null | wc -l)
  if [ -n "$LATEST" ]; then
    WHEN=$(stat -c %y "$LATEST" 2>/dev/null | cut -d. -f1)
    echo "  $agent [enabled=$ENABLED]: last=$WHEN sessions=$COUNT errors=$ERRORS reports=$REPORTS"
  else
    echo "  $agent [enabled=$ENABLED]: NO SESSIONS YET"
  fi
done
echo ""

# Rotation state
if [ -f "$BASE/.rotation_state" ]; then
  IDX=$(cat "$BASE/.rotation_state")
  AGENTS=($(python3 -c "
import json
c = json.load(open('$BASE/config.json'))
enabled = [a for a in c['rotation'] if c['agents'][a]['enabled']]
print(' '.join(enabled))
" 2>/dev/null))
  NEXT_IDX=$((IDX % ${#AGENTS[@]}))
  echo "  Next up: ${AGENTS[$NEXT_IDX]} (slot $((NEXT_IDX+1))/${#AGENTS[@]})"
else
  echo "  Next up: first in rotation (no state yet)"
fi

# Memory stats
echo ""
echo "=== Memory Status ==="
MEMORY_ENABLED=$(python3 -c "import json; c=json.load(open('$BASE/config.json')); print(c.get('memory_enabled', False))" 2>/dev/null || echo "false")
if [ "$MEMORY_ENABLED" = "True" ] || [ "$MEMORY_ENABLED" = "true" ]; then
  # Check if DB is reachable
  DB_UP=$(python3 -c "
import os, sys
sys.path.insert(0, '$BASE/shared/drift-memory')
os.environ.setdefault('DRIFT_DB_HOST', 'localhost')
os.environ.setdefault('DRIFT_DB_PORT', '5433')
os.environ.setdefault('DRIFT_DB_NAME', 'agent_memory')
os.environ.setdefault('DRIFT_DB_USER', 'drift_admin')
os.environ.setdefault('DRIFT_DB_PASSWORD', 'drift_agents_local_dev')
from db_adapter import is_db_active
print('up' if is_db_active() else 'down')
" 2>/dev/null || echo "down")

  if [ "$DB_UP" = "up" ]; then
    echo "  Database: UP (localhost:5433)"
    for agent in max beth susan; do
      STATS=$(python3 -c "
import os, sys
sys.path.insert(0, '$BASE/shared/drift-memory')
os.environ['DRIFT_DB_SCHEMA'] = '$agent'
os.environ.setdefault('DRIFT_DB_HOST', 'localhost')
os.environ.setdefault('DRIFT_DB_PORT', '5433')
os.environ.setdefault('DRIFT_DB_NAME', 'agent_memory')
os.environ.setdefault('DRIFT_DB_USER', 'drift_admin')
os.environ.setdefault('DRIFT_DB_PASSWORD', 'drift_agents_local_dev')
from db_adapter import MemoryDB
db = MemoryDB(schema='$agent')
s = db.get_stats()
last = s['last_memory'] or 'never'
print(f\"memories={s['total']} (core={s['core']} active={s['active']}) embeddings={s['embeddings']} last={last[:19]}\")
" 2>/dev/null || echo "error")
      echo "  $agent: $STATS"
    done
  else
    echo "  Database: DOWN"
  fi
else
  echo "  Memory: DISABLED (set memory_enabled=true in config.json)"
fi
