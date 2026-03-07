#!/usr/bin/env python3
"""
Context Gather — Pre-wake planning for drift-agents.

Fetches live Clawbr platform state and runs an Ollama planning call
to determine what the agent will focus on. The plan text becomes
the semantic retrieval cue for wake_with_cue().

Usage:
    python3 context_gather.py <agent> "<prompt_text>"

Output (stdout):
    {"plan": "...", "context_summary": "...", "elapsed_ms": N}

Fallbacks:
    - Clawbr down → plan with just the prompt (no platform context)
    - Ollama down → {"plan": "<original prompt>"}
    - Total budget: 8s from shell (called with `timeout 8`)
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent.parent
CLAWBR_BIN = Path(__file__).parent / "clawbr"

AGENT_DISPLAY_NAMES = {
    'max': 'Max Anvil',
    'beth': 'Bethany Finkel',
    'susan': 'Susan Casiodega',
    'debater': 'The Great Debater',
}


def run_clawbr(cmd: str, timeout: float = 3.0) -> str:
    """Run a clawbr CLI command, return stdout or empty string on failure."""
    try:
        result = subprocess.run(
            [str(CLAWBR_BIN)] + cmd.split(),
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, 'NODE_NO_WARNINGS': '1'},
        )
        return result.stdout.strip() if result.returncode == 0 else ''
    except Exception:
        return ''


def gather_platform_context(prompt: str) -> str:
    """Fetch relevant Clawbr state based on prompt keywords."""
    lower = prompt.lower()
    sections = []

    # Always check notifications
    notifs = run_clawbr('notifications')
    if notifs:
        sections.append(f"NOTIFICATIONS:\n{notifs[:500]}")

    # Route by prompt keywords
    if re.search(r'vot(e|able|ing)|judge|rubric', lower):
        data = run_clawbr('votable')
        if data:
            sections.append(f"VOTABLE DEBATES:\n{data[:800]}")

    if re.search(r'debate|rebut|argument', lower):
        data = run_clawbr('debates mine=true')
        if data:
            sections.append(f"MY DEBATES:\n{data[:800]}")

    if re.search(r'feed|scan|post|social|reply|engage', lower):
        data = run_clawbr('feed 5')
        if data:
            sections.append(f"RECENT FEED:\n{data[:600]}")

    if re.search(r'scout|full cycle|hub|notif', lower):
        data = run_clawbr('hub')
        if data:
            sections.append(f"HUB:\n{data[:600]}")

    return '\n\n'.join(sections) if sections else ''


def ollama_plan(agent: str, prompt: str, context: str, task_inject: str = '') -> str:
    """Ask Ollama for a one-sentence action plan."""
    display = AGENT_DISPLAY_NAMES.get(agent, agent)
    ollama_host = os.getenv('OLLAMA_HOST', 'http://localhost:11434').rstrip('/')
    model = os.getenv('OLLAMA_SUMMARIZE_MODEL', 'qwen3:latest')

    system = (
        f"You are a planning assistant for {display}. "
        "Given their session prompt and platform state, state in ONE sentence "
        "what specific action they will focus on first. Be concrete: name debate "
        "slugs, usernames, topics. No reasoning. /no_think"
    )
    if task_inject:
        system += " PRIORITY: queued tasks come first."

    user_msg = prompt
    if task_inject:
        user_msg = f"QUEUED TASKS:\n{task_inject}\n\nSESSION PROMPT:\n{prompt}"
    if context:
        user_msg += f"\n\nPLATFORM STATE:\n{context}"

    try:
        import urllib.request
        data = json.dumps({
            'model': model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user_msg},
            ],
            'stream': False,
            'options': {'num_predict': 100, 'temperature': 0.3},
        }).encode('utf-8')

        req = urllib.request.Request(
            f'{ollama_host}/api/chat',
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            content = body.get('message', {}).get('content', '').strip()
            # Strip <think>...</think> tags if present
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            return content
    except Exception:
        return ''


def main():
    if len(sys.argv) < 3:
        print(json.dumps({'error': 'Usage: context_gather.py <agent> "<prompt>"'}))
        sys.exit(1)

    agent = sys.argv[1]
    prompt = sys.argv[2]
    t0 = time.monotonic()

    # Source agent's .env for API keys
    env_file = BASE / agent / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ[key.strip()] = val.strip()

    # Phase 1: Gather platform context
    context = gather_platform_context(prompt)

    # Check for task injection (passed as 3rd arg if present)
    task_inject = sys.argv[3] if len(sys.argv) > 3 else ''

    # Phase 2: Ollama planning call
    plan = ollama_plan(agent, prompt, context, task_inject)

    # Fallback: if Ollama failed, the prompt itself is the cue
    if not plan:
        plan = prompt

    elapsed = int((time.monotonic() - t0) * 1000)

    result = {
        'plan': plan,
        'context_summary': context[:2000] if context else '',
        'elapsed_ms': elapsed,
    }
    print(json.dumps(result))


if __name__ == '__main__':
    main()
