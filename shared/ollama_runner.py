#!/usr/bin/env python3
"""
Ollama Agent Runner — Tool-calling agent loop for Ollama-served models.

Implements the same session lifecycle as `claude --dangerously-skip-permissions`
but routes through Ollama's /api/chat endpoint with tool calling.

Usage:
    python3 ollama_runner.py <agent_dir> <prompt_file> <model> [--max-turns 15] [--timeout 450]

Output:
    Session transcript to stdout (plain text, same format memory_wrapper can consume).
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import re
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Tool definitions (Ollama format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return its output (stdout + stderr). Use this for clawbr commands, web searches, file operations, and any shell task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Use for reading reports, configs, or any text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read (relative to agent directory or absolute)"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates parent directories if needed. Restricted to agent directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to agent directory or absolute within it)"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    }
]

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def resolve_path(path_str: str, agent_dir: Path) -> Path:
    """Resolve a path relative to agent_dir if not absolute."""
    p = Path(path_str)
    if not p.is_absolute():
        p = agent_dir / p
    return p.resolve()


def is_safe_write_path(resolved: Path, agent_dir: Path) -> bool:
    """Only allow writes inside the agent's own directory tree."""
    try:
        resolved.relative_to(agent_dir.resolve())
        return True
    except ValueError:
        return False


def execute_tool(name: str, args: dict, agent_dir: Path, env: dict, cmd_timeout: int = 30) -> str:
    """Execute a tool call and return the result as a string."""
    try:
        if name == "bash":
            command = args.get("command", "")
            if not command:
                return "[error] No command provided"
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=cmd_timeout,
                cwd=str(agent_dir),
                env=env,
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += result.stderr
            if not output.strip():
                output = f"[exit code {result.returncode}]"
            # Truncate very long output
            if len(output) > 8000:
                output = output[:7500] + f"\n... [truncated, {len(output)} chars total]"
            return output

        elif name == "read_file":
            path = resolve_path(args.get("path", ""), agent_dir)
            if not path.exists():
                return f"[error] File not found: {path}"
            size = path.stat().st_size
            if size > 50_000:
                return f"[error] File too large ({size} bytes, limit 50KB)"
            return path.read_text(errors="replace")

        elif name == "write_file":
            path = resolve_path(args.get("path", ""), agent_dir)
            if not is_safe_write_path(path, agent_dir):
                return f"[error] Write denied — path outside agent directory: {path}"
            content = args.get("content", "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return f"[ok] Wrote {len(content)} chars to {path}"

        else:
            return f"[error] Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        return f"[error] Command timed out after {cmd_timeout}s"
    except Exception as e:
        return f"[error] {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Ollama API
# ---------------------------------------------------------------------------

def ollama_chat(url: str, api_key: str, model: str, messages: list, tools: list,
                num_predict: int = 4096, temperature: float = 0.7) -> dict:
    """Send a chat request to Ollama's /api/chat endpoint."""
    endpoint = url.rstrip("/") + "/api/chat"

    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "think": False,
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
        }
    }

    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Ollama API HTTP {e.code}: {body_text}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama API connection error: {e.reason}") from e


# ---------------------------------------------------------------------------
# Text-based tool call parser (fallback for models that don't use structured tool calls)
# ---------------------------------------------------------------------------

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

def parse_text_tool_calls(text: str) -> list:
    """
    Extract tool calls from plain text output.
    Handles patterns like:
      {"name": "bash", "arguments": {"command": "echo hello"}}
      ```json\n{"name": "bash", ...}\n```
    """
    calls = []

    # Find JSON objects that look like tool calls
    # Match {"name": "...", "arguments": ...}
    for m in re.finditer(r'\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*"arguments"\s*:\s*(\{[^}]*\})[^{}]*\}', text):
        name = m.group(1)
        if name not in TOOL_NAMES:
            continue
        try:
            args = json.loads(m.group(2))
            calls.append({"function": {"name": name, "arguments": args}})
        except json.JSONDecodeError:
            continue

    # Also catch ```bash\n<command>\n``` blocks as bash tool calls (common pattern)
    if not calls:
        for m in re.finditer(r'```(?:bash|sh|shell)\n(.+?)\n```', text, re.DOTALL):
            cmd = m.group(1).strip()
            if cmd and len(cmd) < 2000:
                calls.append({"function": {"name": "bash", "arguments": {"command": cmd}}})

    return calls


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_think_only(agent_dir: str, prompt_file: str, model: str, timeout: int = 120):
    """
    Think-only mode: send prompt to Ollama WITHOUT tools.
    The model outputs its plan/intended actions as plain text.
    Used as Phase 1 of hybrid mode (Qwen thinks, Claude executes).
    """
    agent_path = Path(agent_dir).resolve()

    claude_md = agent_path / "CLAUDE.md"
    if not claude_md.exists():
        print(f"[error] No CLAUDE.md found in {agent_path}", file=sys.stderr)
        sys.exit(1)
    system_prompt = claude_md.read_text()

    prompt_path = Path(prompt_file)
    if not prompt_path.exists():
        print(f"[error] Prompt file not found: {prompt_file}", file=sys.stderr)
        sys.exit(1)
    user_prompt = prompt_path.read_text()

    ollama_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    for suffix in ("/api/chat", "/api/ollama"):
        if ollama_url.endswith(suffix):
            ollama_url = ollama_url[:-len(suffix)]
    api_key = os.environ.get("OLLAMA_API_KEY", "")

    # Append instruction to output actionable plan
    user_prompt += "\n\nIMPORTANT: Write out exactly what you plan to do, step by step. For each action, write the exact shell command you would run (e.g. clawbr post \"...\", clawbr feed 10, etc). Write the FULL text of any posts, replies, or debate arguments. Be specific — your plan will be executed by a separate tool-calling system."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    print(f"[ollama_runner] THINK-ONLY mode: model={model} agent={agent_path.name}",
          file=sys.stderr)

    try:
        response = ollama_chat(ollama_url, api_key, model, messages, tools=[],
                               num_predict=4096, temperature=0.7)
        content = response.get("message", {}).get("content", "")
        print(content)
    except Exception as e:
        print(f"[ollama_runner] API error in think-only: {e}", file=sys.stderr)
        sys.exit(1)


def run_agent(agent_dir: str, prompt_file: str, model: str,
              max_turns: int = 15, timeout: int = 450):
    """
    Run the Ollama agent loop.

    1. Read CLAUDE.md as system prompt
    2. Read prompt_file as user prompt
    3. Loop: send to Ollama → execute tool calls → repeat until done
    4. Print transcript to stdout
    """
    agent_path = Path(agent_dir).resolve()
    start_time = time.time()

    # Read system prompt from CLAUDE.md
    claude_md = agent_path / "CLAUDE.md"
    if not claude_md.exists():
        print(f"[error] No CLAUDE.md found in {agent_path}", file=sys.stderr)
        sys.exit(1)
    system_prompt = claude_md.read_text()

    # Read user prompt
    prompt_path = Path(prompt_file)
    if not prompt_path.exists():
        print(f"[error] Prompt file not found: {prompt_file}", file=sys.stderr)
        sys.exit(1)
    user_prompt = prompt_path.read_text()

    # Build environment for tool execution (inherit agent's sourced env)
    tool_env = dict(os.environ)

    # Ollama connection config
    ollama_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    # Strip /api/chat or /api/ollama suffix if present — we add /api/chat ourselves
    for suffix in ("/api/chat", "/api/ollama"):
        if ollama_url.endswith(suffix):
            ollama_url = ollama_url[:-len(suffix)]
    api_key = os.environ.get("OLLAMA_API_KEY", "")

    # Messages
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    transcript_lines = []

    print(f"[ollama_runner] model={model} agent={agent_path.name} max_turns={max_turns}",
          file=sys.stderr)

    for turn in range(max_turns):
        elapsed = time.time() - start_time
        if elapsed > timeout:
            print(f"[ollama_runner] Session timeout after {elapsed:.0f}s", file=sys.stderr)
            break

        print(f"[ollama_runner] Turn {turn + 1}/{max_turns} ({elapsed:.0f}s elapsed)",
              file=sys.stderr)

        try:
            response = ollama_chat(ollama_url, api_key, model, messages, tools=TOOLS)
        except Exception as e:
            print(f"[ollama_runner] API error: {e}", file=sys.stderr)
            transcript_lines.append(f"[Session ended: API error]")
            break

        msg = response.get("message", {})
        messages.append(msg)

        # Record assistant text
        content = msg.get("content", "")
        if content:
            transcript_lines.append(content)

        # Check for tool calls (structured or text-based fallback)
        tool_calls = msg.get("tool_calls")
        if not tool_calls and content:
            # Fallback: some models (qwen2.5-coder) emit tool calls as text
            tool_calls = parse_text_tool_calls(content)
            if tool_calls:
                print(f"[ollama_runner]   (parsed {len(tool_calls)} tool call(s) from text)",
                      file=sys.stderr)

        if not tool_calls:
            # No tool calls — agent is done
            print(f"[ollama_runner] Agent done (no tool calls) after turn {turn + 1}",
                  file=sys.stderr)
            break

        # Execute each tool call
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"command": args} if name == "bash" else {}

            print(f"[ollama_runner]   tool: {name}({json.dumps(args)[:120]})",
                  file=sys.stderr)

            result = execute_tool(name, args, agent_path, tool_env, cmd_timeout=30)
            messages.append({"role": "tool", "content": result})

    # Output transcript to stdout
    print("\n".join(transcript_lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ollama Agent Runner for drift-agents")
    parser.add_argument("agent_dir", help="Path to agent directory (e.g., gerald/)")
    parser.add_argument("prompt_file", help="Path to prompt text file")
    parser.add_argument("model", help="Ollama model name (e.g., kimi-k2.5)")
    parser.add_argument("--max-turns", type=int, default=15,
                        help="Maximum tool-calling turns (default: 15)")
    parser.add_argument("--timeout", type=int, default=450,
                        help="Session timeout in seconds (default: 450)")
    parser.add_argument("--think-only", action="store_true",
                        help="Think-only mode: output plan text, no tool execution (Phase 1 of hybrid)")
    args = parser.parse_args()

    if args.think_only:
        run_think_only(
            agent_dir=args.agent_dir,
            prompt_file=args.prompt_file,
            model=args.model,
            timeout=args.timeout,
        )
        return

    run_agent(
        agent_dir=args.agent_dir,
        prompt_file=args.prompt_file,
        model=args.model,
        max_turns=args.max_turns,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
