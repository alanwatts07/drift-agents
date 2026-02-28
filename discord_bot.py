#!/usr/bin/env python3
"""Discord task bridge for drift-agents.

Routes Discord messages to agent task queues. Reports completions back.

Usage:
  python discord_bot.py

Messages in Discord:
  max: go audit the latest AI debate
  beth: write a post about Borges and debate culture
  susan: judge all open debates today

Bot reacts with a mailbox emoji when queued, posts the agent's response
back to the channel when done.

Config: discord_bot.env
"""

import asyncio
import json
import os
import time
from pathlib import Path

import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "discord_bot.env")

BASE = Path(__file__).parent
AGENTS = ["max", "beth", "susan", "debater"]
AGENT_DISPLAY = {
    "max": "Max Anvil",
    "beth": "Bethany Finkel",
    "susan": "Susan Casiodega",
    "debater": "The Great Debater",
}
ALIASES = {
    "max": "max", "max_anvil": "max", "anvil": "max",
    "beth": "beth", "bethany": "beth", "finkel": "beth",
    "susan": "susan", "casiodega": "susan", "judge": "susan",
    "deb": "debater", "debater": "debater", "great_debater": "debater",
    "the_great_debater": "debater", "debate": "debater",
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

# Optional: restrict to specific channel IDs (comma-separated in env)
ALLOWED_CHANNELS = set()
raw = os.environ.get("DISCORD_CHANNEL_IDS", "")
if raw:
    ALLOWED_CHANNELS = {int(x.strip()) for x in raw.split(",") if x.strip()}


@client.event
async def on_ready():
    print(f"Bot online as {client.user} — listening for tasks")
    check_completions.start()


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

    # Route by channel name if no prefix
    if not agent:
        chan = message.channel.name.lower()
        for a in AGENTS:
            if a in chan:
                agent = a
                task = text
                break

    if not agent:
        await message.reply("Which agent? Start with `max:`, `beth:`, `susan:`, or `deb:` (or `all:` for everyone)")
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
