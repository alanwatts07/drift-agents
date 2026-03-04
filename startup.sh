#!/bin/bash
# Drift Agents — Full Stack Startup
# Usage: ./startup.sh [--status]
BASE="$HOME/Hackstuff/drift-agents"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
fail() { echo -e "  ${RED}[DOWN]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; }

status_only=false
[[ "$1" == "--status" ]] && status_only=true

echo "=== Drift Agents Health Check ==="

# 1. Docker containers
pg_up=$(docker inspect drift-agents-db --format '{{.State.Running}}' 2>/dev/null)
neo_up=$(docker inspect drift-agents-graph --format '{{.State.Running}}' 2>/dev/null)

if [ "$pg_up" = "true" ]; then ok "PostgreSQL"; else fail "PostgreSQL"; fi
if [ "$neo_up" = "true" ]; then ok "Neo4j"; else fail "Neo4j"; fi

# 2. Ollama
if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    model_count=$(curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; print(len(json.load(sys.stdin)['models']))" 2>/dev/null || echo "?")
    ok "Ollama ($model_count models)"
else
    fail "Ollama"
fi

# 3. Discord bot
bot_pid=$(pgrep -f "python3.*discord_bot.py" | head -1)
if [ -n "$bot_pid" ]; then
    ok "Discord bot (PID $bot_pid)"
else
    fail "Discord bot"
fi

# 4. Cron
if crontab -l 2>/dev/null | grep -q "drift-agents/run.sh"; then
    schedule=$(crontab -l 2>/dev/null | grep "drift-agents/run.sh" | awk '{print $1, $2}')
    ok "Cron ($schedule)"
else
    fail "Cron (no drift-agents entry)"
fi

# 5. Lock files
locks=$(ls /tmp/drift-agent-*.lock 2>/dev/null)
if [ -n "$locks" ]; then
    for lock in $locks; do
        agent=$(basename "$lock" | sed 's/drift-agent-//;s/\.lock//')
        pid=$(cat "$lock" 2>/dev/null)
        if kill -0 "$pid" 2>/dev/null; then
            warn "Lock: $agent (PID $pid running)"
        else
            warn "Lock: $agent (stale — PID $pid dead)"
        fi
    done
else
    ok "No lock files"
fi

echo ""

if $status_only; then exit 0; fi

# --- Bring up anything that's down ---

if [ "$pg_up" != "true" ] || [ "$neo_up" != "true" ]; then
    echo "Starting Docker services..."
    cd "$BASE" && docker compose up -d 2>&1 | grep -v "level=warning"
    sleep 5
    ok "Docker services started"
fi

if [ -z "$bot_pid" ]; then
    echo "Starting Discord bot..."
    cd "$BASE" && nohup python3 -u discord_bot.py > discord_bot.log 2>&1 &
    sleep 3
    new_pid=$(pgrep -f "python3.*discord_bot.py" | head -1)
    if [ -n "$new_pid" ]; then
        ok "Discord bot started (PID $new_pid)"
    else
        fail "Discord bot failed to start — check discord_bot.log"
    fi
fi

if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "Starting Ollama..."
    ollama serve > /dev/null 2>&1 &
    sleep 3
    if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        ok "Ollama started"
    else
        fail "Ollama failed to start"
    fi
fi

# Clean stale locks
for lock in /tmp/drift-agent-*.lock; do
    [ -f "$lock" ] || continue
    pid=$(cat "$lock" 2>/dev/null)
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$lock"
        warn "Cleaned stale lock: $(basename "$lock")"
    fi
done

echo ""
echo "=== All systems go ==="
