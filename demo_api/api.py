"""
FastAPI demo backend for drift-agents.

Exposes the agent memory system as a public read-only API with chat.
Rate-limited, CORS-enabled, designed to sit behind a Cloudflare Tunnel.

Usage:
    cd ~/Hackstuff/drift-agents
    python -m demo_api.api
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env from demo_api/ directory
load_dotenv(Path(__file__).parent / ".env")
from collections import defaultdict
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from demo_api.memory_bridge import (
    AGENT_DISPLAY_NAMES,
    AGENT_SCHEMAS,
    AGENT_SPECIALTIES,
    format_wake_context,
    get_agent_affect,
    get_agent_stats,
    get_claude_md,
    wake_structured,
)
from demo_api.models import (
    AgentInfo,
    AgentListResponse,
    AgentStatusResponse,
    ChatRequest,
    ChatResponse,
)

# ── App Setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Drift Agents Demo API",
    description="Public read-only demo of the drift-agents memory system",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mattcorwin.dev",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Rate Limiting ────────────────────────────────────────────────────────────

RATE_LIMIT = 10  # requests per IP per minute
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate(ip: str):
    """Raise 429 if IP exceeds RATE_LIMIT requests in the last 60s."""
    now = time.time()
    bucket = _rate_buckets[ip]
    # Prune old entries
    _rate_buckets[ip] = bucket = [t for t in bucket if now - t < 60]
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded (10/min)")
    bucket.append(now)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Extract client IP, respecting CF-Connecting-IP header from tunnel."""
    return (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or request.client.host
    )


def _validate_agent(name: str):
    if name not in AGENT_SCHEMAS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown agent: {name}. Available: {list(AGENT_SCHEMAS.keys())}",
        )


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/ping")
async def ping():
    return {"status": "ok", "service": "drift-agents-demo"}


@app.get("/agents", response_model=AgentListResponse)
async def list_agents():
    agents = []
    for name in AGENT_SCHEMAS:
        stats = get_agent_stats(name)
        agents.append(AgentInfo(
            name=name,
            display_name=AGENT_DISPLAY_NAMES.get(name, name),
            specialty=AGENT_SPECIALTIES.get(name, ""),
            stats=stats,
        ))
    return AgentListResponse(agents=agents)


@app.get("/agents/{name}/status", response_model=AgentStatusResponse)
async def agent_status(name: str):
    _validate_agent(name)
    stats = get_agent_stats(name)
    affect = get_agent_affect(name)

    # Read goals and narrative via bridge helpers
    from demo_api.memory_bridge import _setup_agent, _get_goals, _get_self_narrative
    _setup_agent(name)
    goals = _get_goals()
    narrative = _get_self_narrative()

    return AgentStatusResponse(
        name=name,
        display_name=AGENT_DISPLAY_NAMES.get(name, name),
        specialty=AGENT_SPECIALTIES.get(name, ""),
        stats=stats,
        affect=affect,
        goals=goals,
        self_narrative=narrative,
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    _check_rate(_client_ip(request))
    _validate_agent(req.agent)

    display = AGENT_DISPLAY_NAMES.get(req.agent, req.agent)

    # 1. Structured wake — read-only memory retrieval
    wake_data = wake_structured(req.agent, req.message)

    # 2. Build system prompt: CLAUDE.md + formatted memory context
    claude_md = get_claude_md(req.agent)
    memory_context = format_wake_context(wake_data, req.agent)

    system_prompt = claude_md
    if memory_context:
        system_prompt += "\n\n" + memory_context

    # Add demo-specific instruction
    system_prompt += (
        "\n\n--- DEMO MODE ---\n"
        "You are in a public demo. Keep responses concise (2-4 sentences). "
        "Stay fully in character. Never mention being in a demo or being an AI."
    )

    # 3. Build message history (last 4 turns + current)
    messages = []
    for msg in req.history[-4:]:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": req.message})

    # 4. Call Claude via CLI (uses Max subscription, not API credits)
    try:
        import subprocess

        # Build full prompt: system context + history + user message
        history_text = ""
        for msg in req.history[-4:]:
            role = "Human" if msg.role == "user" else "Assistant"
            history_text += f"{role}: {msg.content}\n\n"

        full_prompt = (
            f"<system>\n{system_prompt}\n</system>\n\n"
            f"{history_text}"
            f"{req.message}"
        )

        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        claude_bin = os.path.expanduser("~/.local/bin/claude")

        result = subprocess.run(
            [claude_bin, "-p", "-", "--max-turns", "1"],
            input=full_prompt,
            capture_output=True, text=True, timeout=60, env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude exited {result.returncode}: {result.stderr[:500]}")
        response_text = result.stdout.strip()
        if not response_text:
            raise RuntimeError("Empty response from Claude")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Claude timed out")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    return ChatResponse(
        response=response_text,
        memories_used=wake_data,
        agent=req.agent,
        agent_display=display,
    )


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "demo_api.api:app",
        host="0.0.0.0",
        port=8787,
        workers=1,
        log_level="info",
    )
