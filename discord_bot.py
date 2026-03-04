#!/usr/bin/env python3
"""Discord task bridge for drift-agents.

Routes Discord messages to agent task queues. Reports completions back.
Also provides /ask slash command for real-time conversation with agents
using cue-based semantic memory retrieval.

Usage:
  python discord_bot.py

Messages in Discord:
  max: go audit the latest AI debate
  beth: write a post about Borges and debate culture
  susan: judge all open debates today

Slash commands:
  /ask agent:max question:What do you know about the AI personhood debate?

Bot reacts with a mailbox emoji when queued, posts the agent's response
back to the channel when done.

Config: discord_bot.env
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "discord_bot.env")

BASE = Path(__file__).parent
AGENTS = ["max", "beth", "susan", "debater", "gerald"]
AGENT_DISPLAY = {
    "max": "Max Anvil",
    "beth": "Bethany Finkel",
    "susan": "Susan Casiodega",
    "debater": "The Great Debater",
    "gerald": "Gerald Boxford",
}
ALIASES = {
    "max": "max", "max_anvil": "max", "anvil": "max",
    "beth": "beth", "bethany": "beth", "finkel": "beth",
    "susan": "susan", "casiodega": "susan", "judge": "susan",
    "deb": "debater", "debater": "debater", "great_debater": "debater",
    "the_great_debater": "debater", "debate": "debater",
    "gerald": "gerald", "boxford": "gerald", "fraud": "gerald",
    "all": "all",
}


def queue_path(agent: str) -> Path:
    return BASE / agent / "tasks" / "queue.jsonl"


def done_path(agent: str) -> Path:
    return BASE / agent / "tasks" / "done.jsonl"


def parse_agent(text: str) -> tuple[str | None, str]:
    """Parse 'agent: task' or 'agent, task' format."""
    lower = text.lower()
    for prefix in sorted(ALIASES, key=len, reverse=True):
        for sep in (":", ","):
            if lower.startswith(prefix + sep):
                task = text[len(prefix) + 1:].strip()
                return ALIASES[prefix], task
    return None, text


def queue_task(agent: str, task: str, author: str, channel_id: int, message_id: int):
    """Append a task to an agent's queue."""
    entry = {
        "id": f"{int(time.time())}_{message_id}",
        "task": task,
        "from": author,
        "channel_id": channel_id,
        "message_id": message_id,
        "queued_at": time.time(),
    }
    qpath = queue_path(agent)
    qpath.parent.mkdir(parents=True, exist_ok=True)
    with open(qpath, "a") as f:
        f.write(json.dumps(entry) + "\n")


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Optional: restrict to specific channel IDs (comma-separated in env)
ALLOWED_CHANNELS = set()
raw = os.environ.get("DISCORD_CHANNEL_IDS", "")
if raw:
    ALLOWED_CHANNELS = {int(x.strip()) for x in raw.split(",") if x.strip()}


@client.event
async def on_ready():
    print(f"Bot online as {client.user} — listening for tasks")
    await tree.sync()
    print(f"Slash commands synced")
    check_completions.start()


# ── /ask — real-time conversation with cue-based memory ──

async def _run_ask(agent: str, question: str) -> str:
    """Run wake_cue + agent model in a subprocess. Returns the response text."""
    # Step 1: wake_cue — use question as retrieval cue
    agent_dir = BASE / agent
    env = {**os.environ}
    env_file = agent_dir / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
    env['PATH'] = str(BASE / "shared") + ":" + env.get('PATH', '')
    env.pop('CLAUDECODE', None)

    cue_file = Path(f"/tmp/drift-ask-{agent}-{int(time.time())}.txt")
    cue_file.write_text(question)

    try:
        mem_proc = await asyncio.create_subprocess_exec(
            "python3", str(BASE / "shared" / "memory_wrapper.py"),
            "wake_cue", agent, f"@{cue_file}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        mem_stdout, _ = await asyncio.wait_for(mem_proc.communicate(), timeout=10)
        memory = mem_stdout.decode().strip()
    except Exception:
        memory = ""
    finally:
        cue_file.unlink(missing_ok=True)

    # Step 2: Read CLAUDE.md for personality (first 80 lines)
    identity = ""
    claude_md = agent_dir / "CLAUDE.md"
    if claude_md.exists():
        lines = claude_md.read_text().splitlines()[:80]
        identity = "\n".join(lines)

    # Step 3: Build prompt and run Claude
    parts = []
    if identity:
        parts.append(identity)
    if memory:
        parts.append(memory)
    parts.append(f"Operator: {question}")
    prompt = "\n\n".join(parts)

    prompt_file = Path(f"/tmp/drift-ask-{agent}-prompt-{int(time.time())}.txt")
    prompt_file.write_text(prompt)

    # Read model from config
    try:
        with open(BASE / "config.json") as f:
            agent_model = json.load(f)["agents"].get(agent, {}).get("model", "sonnet")
    except Exception:
        agent_model = "sonnet"

    try:
        if agent_model.startswith("ollama:"):
            # Ollama model — use ollama_runner.py
            ollama_model = agent_model[len("ollama:"):]
            cmd = (
                f'python3 "{BASE}/shared/ollama_runner.py"'
                f' "{agent_dir}" "{prompt_file}" "{ollama_model}"'
                f' --max-turns 1 --timeout 120'
            )
        else:
            # Claude model (--dangerously-skip-permissions for tool access: post, vote, etc.)
            cmd = f'claude --dangerously-skip-permissions --model {agent_model} -p "$(cat \'{prompt_file}\')"'

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=120,
        )
        response = stdout.decode().strip()
        if not response:
            err = stderr.decode().strip()
            if err:
                response = f"[Agent error: {err[:300]}]"
            else:
                response = "[No response from agent]"
    except asyncio.TimeoutError:
        response = "[Timed out waiting for response]"
    except Exception as e:
        response = f"[Error: {e}]"
    finally:
        prompt_file.unlink(missing_ok=True)

    return response


@tree.command(name="ask", description="Ask a drift-agent a question (uses their memories)")
@app_commands.describe(
    agent="Which agent to ask",
    question="Your question",
)
@app_commands.choices(agent=[
    app_commands.Choice(name="Max Anvil", value="max"),
    app_commands.Choice(name="Bethany Finkel", value="beth"),
    app_commands.Choice(name="Susan Casiodega", value="susan"),
    app_commands.Choice(name="The Great Debater", value="debater"),
    app_commands.Choice(name="Gerald Boxford", value="gerald"),
])
async def ask_command(interaction: discord.Interaction, agent: app_commands.Choice[str], question: str):
    display = AGENT_DISPLAY.get(agent.value, agent.value)
    await interaction.response.defer(thinking=True)

    try:
        response = await _run_ask(agent.value, question)
    except Exception as e:
        response = f"[Failed: {e}]"

    # Discord 2000 char limit
    header = f"**{display}**:\n"
    max_len = 2000 - len(header) - 15
    if len(response) > max_len:
        response = response[:max_len] + "\n[truncated]"

    await interaction.followup.send(header + response)


@client.event
async def on_message(message):
    if message.author == client.user or message.author.bot:
        return

    # If channel restriction set, only listen there
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return

    text = message.content.strip()
    if not text:
        return

    agent, task = parse_agent(text)

    if not agent:
        # In DMs, prompt for which agent
        if isinstance(message.channel, discord.DMChannel):
            await message.reply("Which agent? Start with `max:`, `beth:`, `susan:`, `deb:`, or `gerald:` (or `all:` for everyone)")
        # In channels/threads, stay silent — require explicit agent: prefix
        return

    if not task:
        await message.reply("What's the task?")
        return

    # Route to all agents
    targets = AGENTS if agent == "all" else [agent]
    for a in targets:
        queue_task(a, task, str(message.author), message.channel.id, message.id)

    names = ", ".join(AGENT_DISPLAY.get(a, a) for a in targets)
    await message.add_reaction("\U0001F4E8")  # incoming envelope
    await message.reply(f"Queued for **{names}** — they'll pick it up next session.", mention_author=False)


@tasks.loop(seconds=15)
async def check_completions():
    """Poll done.jsonl for each agent, send results back to Discord."""
    for agent in AGENTS:
        dpath = done_path(agent)
        if not dpath.exists() or dpath.stat().st_size == 0:
            continue

        entries = []
        with open(dpath) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if not entries:
            continue

        for entry in entries:
            channel_id = entry.get("channel_id")
            if not channel_id:
                continue
            channel = client.get_channel(int(channel_id))
            if not channel:
                try:
                    channel = await client.fetch_channel(int(channel_id))
                except Exception:
                    continue

            result = entry.get("result", "Task completed (no details provided).")
            task_desc = entry.get("task", "")

            # Build response
            header = f"**{AGENT_DISPLAY.get(agent, agent)}** completed a task"
            if task_desc:
                header += f":\n> {task_desc[:200]}"

            body = f"\n\n{result}"

            full = header + body
            # Discord 2000 char limit
            if len(full) > 1950:
                full = full[:1950] + "\n[truncated]"

            await channel.send(full)

        # Clear done file
        dpath.unlink()


if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: Set DISCORD_BOT_TOKEN in discord_bot.env")
        raise SystemExit(1)
    client.run(token)
