#!/bin/bash
# Run The Great Debater independently of rotation
BASE="$HOME/Hackstuff/drift-agents"
echo "$(date -Iseconds) Running debater (standalone)"
"$BASE/run_agent.sh" debater
