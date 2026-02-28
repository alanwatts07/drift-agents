#!/usr/bin/env python3
"""
Memory Wrapper — Wake/Sleep lifecycle bridge for drift-agents.

Called by run_agent.sh to provide persistent memory across sessions.
Uses drift-memory modules (adapted for local Ollama + direct psycopg2).

Usage:
    python memory_wrapper.py wake <agent>                    # stdout: memory context preamble
    python memory_wrapper.py sleep <agent> <transcript.log>  # consolidate session
    python memory_wrapper.py status <agent>                  # memory stats
    python memory_wrapper.py search <agent> <query>          # ad-hoc search

Environment:
    DRIFT_DB_HOST, DRIFT_DB_PORT, DRIFT_DB_NAME,
    DRIFT_DB_USER, DRIFT_DB_PASSWORD, DRIFT_DB_SCHEMA
    OLLAMA_HOST, OLLAMA_EMBED_MODEL, OLLAMA_SUMMARIZE_MODEL
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add drift-memory to path
DRIFT_MEMORY_DIR = Path(__file__).parent / "drift-memory"
if str(DRIFT_MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(DRIFT_MEMORY_DIR))

# Agent name -> schema mapping
AGENT_SCHEMAS = {
    'max': 'max',
    'beth': 'beth',
    'susan': 'susan',
}

AGENT_DISPLAY_NAMES = {
    'max': 'Max Anvil',
    'beth': 'Bethany Finkel',
    'susan': 'Susan Casiodega',
}


def setup_env(agent: str):
    """Set DB schema env var for the agent."""
    schema = AGENT_SCHEMAS.get(agent, agent)
    os.environ['DRIFT_DB_SCHEMA'] = schema
    # Reset singleton so it picks up new schema
    from db_adapter import reset_db
    reset_db()


# ============================================================
# WAKE — retrieve memories, output context preamble
# ============================================================

def wake(agent: str) -> str:
    """
    Retrieve agent's memories + shared memories.
    Returns formatted context preamble (printed to stdout).
    """
    setup_env(agent)
    from db_adapter import get_db, db_to_file_metadata

    db = get_db()
    display_name = AGENT_DISPLAY_NAMES.get(agent, agent)
    lines = []
    lines.append(f"=== YOUR MEMORY ({display_name}) ===")

    try:
        stats = db.get_stats()
    except Exception as e:
        lines.append(f"[Memory system unavailable: {e}]")
        lines.append("===")
        return '\n'.join(lines)

    if stats['total'] == 0:
        lines.append("[No memories yet — this is your first session with persistent memory.]")
        lines.append(f"Total memories: 0")
        lines.append("===")
        return '\n'.join(lines)

    # Recent memories (last 5, by creation time)
    recent = db.list_memories(type_='active', limit=5)
    if recent:
        lines.append("")
        for row in recent:
            meta, content = db_to_file_metadata(row)
            tags = meta.get('tags', [])
            tag_str = ', '.join(tags[:3]) if tags else ''
            preview = content[:150].replace('\n', ' ')
            label = 'Recent'
            if 'lesson' in tags:
                label = 'Lesson'
            elif 'key-fact' in tags:
                label = 'Fact'
            elif 'thread' in tags:
                label = 'Thread'
            lines.append(f"[{label}] {preview}")

    # Core memories (always included)
    core = db.list_memories(type_='core', limit=3)
    if core:
        lines.append("")
        for row in core:
            meta, content = db_to_file_metadata(row)
            preview = content[:150].replace('\n', ' ')
            lines.append(f"[Core] {preview}")

    # Lessons (high value)
    lessons = []
    try:
        import psycopg2.extras
        with db._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT * FROM {db._table('memories')}
                    WHERE 'lesson' = ANY(tags) AND type IN ('core', 'active')
                    ORDER BY emotional_weight DESC LIMIT 3
                """)
                lessons = [dict(r) for r in cur.fetchall()]
    except Exception:
        pass

    if lessons:
        lines.append("")
        for row in lessons:
            meta, content = db_to_file_metadata(row)
            preview = content[:150].replace('\n', ' ')
            # Don't duplicate if already shown in recent
            if not any(preview[:50] in l for l in lines):
                lines.append(f"[Lesson] {preview}")

    # Shared memories from other agents
    shared_lines = _get_shared_memories(agent, limit=3)
    if shared_lines:
        lines.append("")
        lines.extend(shared_lines)

    # Stats footer
    lines.append("")
    last_session = stats.get('last_memory')
    if last_session:
        try:
            last_dt = datetime.fromisoformat(last_session)
            ago = datetime.now(timezone.utc) - last_dt
            hours = int(ago.total_seconds() / 3600)
            if hours < 1:
                ago_str = f"{int(ago.total_seconds() / 60)}m ago"
            elif hours < 24:
                ago_str = f"{hours}h ago"
            else:
                ago_str = f"{hours // 24}d ago"
        except Exception:
            ago_str = "unknown"
    else:
        ago_str = "never"

    lines.append(f"Total memories: {stats['total']} (core: {stats['core']}, active: {stats['active']}) | "
                 f"Last session: {ago_str} | Sessions: {stats['sessions']}")
    lines.append("===")

    return '\n'.join(lines)


def _get_shared_memories(agent: str, limit: int = 3) -> list[str]:
    """Get recent shared memories from other agents."""
    lines = []
    try:
        import psycopg2.extras
        from db_adapter import get_db
        db = get_db()

        with db._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM shared.memories
                    WHERE created_by != %s
                    ORDER BY created DESC LIMIT %s
                """, (agent, limit))
                rows = cur.fetchall()

        for row in rows:
            source = row.get('created_by', '?')
            display = AGENT_DISPLAY_NAMES.get(source, source)
            content = row.get('content', '')[:150].replace('\n', ' ')
            lines.append(f"[Shared/{display}] {content}")

    except Exception:
        pass  # Shared table might not exist yet or DB issue

    return lines


# ============================================================
# SLEEP — summarize session, store memories
# ============================================================

def sleep(agent: str, transcript_path: str):
    """
    Process session transcript: extract memories, store, cross-pollinate.
    """
    setup_env(agent)

    if not os.path.exists(transcript_path):
        print(f"ERROR: Transcript not found: {transcript_path}", file=sys.stderr)
        return False

    print(f"[memory] Sleep phase for {agent}: {transcript_path}")

    # Extract session text from log file
    session_text = _extract_from_log(transcript_path)
    if not session_text or len(session_text) < 50:
        print(f"[memory] Session too short to summarize ({len(session_text or '')} chars)")
        return False

    print(f"[memory] Extracted {len(session_text)} chars from transcript")

    # Summarize using local Ollama
    try:
        from session_summarizer import summarize_session, parse_extraction
        raw_output, llm_meta = summarize_session(session_text)
        print(f"[memory] Summarized via {llm_meta.get('model', '?')}")
    except Exception as e:
        print(f"[memory] Summarization failed: {e}")
        # Fallback: store raw excerpts
        _store_raw_fallback(agent, session_text)
        return False

    if not raw_output or len(raw_output) < 30:
        print("[memory] No usable output from summarizer")
        _store_raw_fallback(agent, session_text)
        return False

    # Parse structured output
    parsed = parse_extraction(raw_output)
    print(f"[memory] Parsed: {len(parsed['threads'])} threads, "
          f"{len(parsed['lessons'])} lessons, {len(parsed['facts'])} facts")

    if not any(parsed.values()):
        print("[memory] Parser found nothing usable")
        _store_raw_fallback(agent, session_text)
        return False

    # Store memories
    stored_ids = _store_parsed_memories(agent, parsed)
    print(f"[memory] Stored {len(stored_ids)} memories")

    # Cross-pollinate to shared schema
    _cross_pollinate(agent, parsed, stored_ids)

    # Run decay/maintenance
    try:
        from decay_evolution import session_maintenance
        session_maintenance()
    except Exception as e:
        print(f"[memory] Decay maintenance failed (non-fatal): {e}")

    # Update agent registry
    try:
        from db_adapter import get_db
        db = get_db()
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE shared.agent_registry
                    SET last_active = NOW()
                    WHERE schema_name = %s
                """, (AGENT_SCHEMAS.get(agent, agent),))
    except Exception:
        pass

    print(f"[memory] Sleep phase complete for {agent}")
    return True


def _extract_from_log(log_path: str, max_chars: int = 10000) -> str:
    """
    Extract meaningful content from a session log file.
    Handles both plain text logs and JSONL format.
    """
    content = Path(log_path).read_text(encoding='utf-8', errors='replace')

    # Check if it's JSONL (stream-json output)
    if content.strip().startswith('{'):
        return _extract_from_jsonl(content, max_chars)

    # Plain text log — filter out noise
    lines = content.split('\n')
    meaningful = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Skip common noise patterns
        if any(line.startswith(p) for p in (
            'ToolUse:', 'ToolResult:', '⏳', '✓', '⚡', '───',
            'Cost:', 'Duration:', 'Input tokens:', 'Output tokens:',
        )):
            continue
        meaningful.append(line)

    text = '\n'.join(meaningful)

    if len(text) <= max_chars:
        return text

    # Proportional sampling: 40% start, 20% middle, 40% end
    chunk = max_chars // 5
    start = text[:chunk * 2]
    mid_point = len(text) // 2
    middle = text[mid_point - chunk:mid_point + chunk]
    end = text[-chunk * 2:]

    return f"{start}\n\n[... middle of session ...]\n\n{middle}\n\n[... later ...]\n\n{end}"


def _extract_from_jsonl(content: str, max_chars: int = 10000) -> str:
    """Extract from Claude Code JSONL stream output."""
    texts = []
    for line in content.split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        if entry.get('type') == 'assistant':
            msg = entry.get('message', {})
            for block in msg.get('content', []):
                if block.get('type') == 'text':
                    texts.append(f"[ASSISTANT] {block['text']}")
        elif entry.get('type') == 'human':
            msg = entry.get('message', {})
            for block in msg.get('content', []):
                if isinstance(block, dict) and block.get('type') == 'text':
                    text = block['text']
                    if not text.startswith('<system-reminder>'):
                        texts.append(f"[USER] {text}")

    full = '\n'.join(texts)
    if len(full) <= max_chars:
        return full

    chunk = max_chars // 5
    start = full[:chunk * 2]
    mid_point = len(full) // 2
    middle = full[mid_point - chunk:mid_point + chunk]
    end = full[-chunk * 2:]
    return f"{start}\n\n[...]\n\n{middle}\n\n[...]\n\n{end}"


def _store_parsed_memories(agent: str, parsed: dict) -> list[str]:
    """Store extracted threads/lessons/facts as memories."""
    from db_adapter import get_db
    from entity_detection import detect_entities, detect_event_time

    db = get_db()
    stored_ids = []
    session_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    session_tag = f"session-{session_date}"

    import random, string

    def gen_id():
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

    # Store threads
    for thread in parsed.get('threads', []):
        mid = gen_id()
        content = f"[Session {session_date}] Thread: {thread['name']}. {thread['summary']} Status: {thread['status']}."
        tags = ['session-summary', 'thread', session_tag, f"thread-{thread['status']}"]
        emotion = {'completed': 0.65, 'blocked': 0.3, 'in-progress': 0.5}.get(thread['status'], 0.5)
        entities = detect_entities(content, tags)

        db.insert_memory(
            memory_id=mid, type_='active', content=content,
            tags=tags, emotional_weight=emotion, entities=entities,
            importance=0.5, freshness=1.0,
        )
        _embed_memory(db, mid, content)
        stored_ids.append(mid)

    # Store lessons
    for i, lesson in enumerate(parsed.get('lessons', []), 1):
        mid = gen_id()
        content = f"[Session {session_date}] Lesson learned: {lesson}"
        tags = ['session-summary', 'lesson', session_tag, 'heuristic']
        entities = detect_entities(content, tags)

        db.insert_memory(
            memory_id=mid, type_='active', content=content,
            tags=tags, emotional_weight=0.6, entities=entities,
            importance=0.6, freshness=1.0,
        )
        _embed_memory(db, mid, content)
        stored_ids.append(mid)

    # Store facts
    for i, fact in enumerate(parsed.get('facts', []), 1):
        mid = gen_id()
        content = f"[Session {session_date}] Key fact: {fact}"
        tags = ['session-summary', 'key-fact', session_tag, 'procedural']
        entities = detect_entities(content, tags)

        db.insert_memory(
            memory_id=mid, type_='active', content=content,
            tags=tags, emotional_weight=0.5, entities=entities,
            importance=0.5, freshness=1.0,
        )
        _embed_memory(db, mid, content)
        stored_ids.append(mid)

    # Link co-occurrences (same session)
    if len(stored_ids) > 1:
        try:
            with db._conn() as conn:
                with conn.cursor() as cur:
                    for j in range(len(stored_ids)):
                        for k in range(j + 1, len(stored_ids)):
                            for m1, m2 in [(stored_ids[j], stored_ids[k]),
                                           (stored_ids[k], stored_ids[j])]:
                                cur.execute(f"""
                                    INSERT INTO {db._table('co_occurrences')} (memory_id, other_id, count)
                                    VALUES (%s, %s, 1)
                                    ON CONFLICT (memory_id, other_id)
                                    DO UPDATE SET count = {db._table('co_occurrences')}.count + 1
                                """, (m1, m2))
        except Exception as e:
            print(f"[memory] Co-occurrence linking failed: {e}")

    return stored_ids


def _embed_memory(db, memory_id: str, content: str):
    """Embed a memory for semantic search. Fail silently."""
    try:
        from semantic_search import get_embedding
        embedding = get_embedding(content)
        if embedding:
            db.upsert_embedding(memory_id, embedding, preview=content[:200])
    except Exception:
        pass  # Embedding failure shouldn't block storage


def _store_raw_fallback(agent: str, session_text: str):
    """Fallback: store raw session excerpts when summarizer fails."""
    from db_adapter import get_db
    import random, string

    db = get_db()
    session_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Take first and last 500 chars as a raw memory
    if len(session_text) > 1200:
        excerpt = session_text[:500] + "\n\n[...]\n\n" + session_text[-500:]
    else:
        excerpt = session_text

    mid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    content = f"[Session {session_date}] Raw session excerpt (summarizer failed):\n{excerpt}"

    db.insert_memory(
        memory_id=mid, type_='active', content=content,
        tags=['session-summary', 'raw-excerpt', f'session-{session_date}'],
        emotional_weight=0.3, importance=0.3, freshness=1.0,
    )
    print(f"[memory] Stored raw fallback: {mid}")


def _cross_pollinate(agent: str, parsed: dict, stored_ids: list):
    """
    Copy platform-relevant and inter-agent items to shared.memories.
    Items mentioning other agents or platform-wide topics get shared.
    """
    from db_adapter import get_db
    import random, string

    db = get_db()
    session_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Keywords that indicate cross-agent relevance
    SHARED_KEYWORDS = {
        'clawbr', 'debate', 'tournament', 'platform', 'community',
        'all agents', 'everyone', 'max', 'beth', 'susan',
        'bethany', 'max anvil', 'susan casiodega',
    }

    other_agents = {a for a in AGENT_SCHEMAS if a != agent}

    items_to_share = []

    # Check threads
    for thread in parsed.get('threads', []):
        text = (thread.get('summary', '') + ' ' + thread.get('name', '')).lower()
        if any(kw in text for kw in SHARED_KEYWORDS) or any(a in text for a in other_agents):
            items_to_share.append(
                f"[{AGENT_DISPLAY_NAMES.get(agent, agent)}] Thread: {thread['name']}. {thread['summary']}"
            )

    # Check lessons (always share lessons — they're high value)
    for lesson in parsed.get('lessons', []):
        items_to_share.append(
            f"[{AGENT_DISPLAY_NAMES.get(agent, agent)}] Lesson: {lesson}"
        )

    # Check facts for cross-agent relevance
    for fact in parsed.get('facts', []):
        text = fact.lower()
        if any(kw in text for kw in SHARED_KEYWORDS) or any(a in text for a in other_agents):
            items_to_share.append(
                f"[{AGENT_DISPLAY_NAMES.get(agent, agent)}] Fact: {fact}"
            )

    if not items_to_share:
        return

    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                for content in items_to_share:
                    mid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
                    cur.execute("""
                        INSERT INTO shared.memories
                        (id, content, created_by, tags, emotional_weight, importance)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                    """, (mid, content, agent,
                          ['cross-agent', f'session-{session_date}', f'from-{agent}'],
                          0.5, 0.5))

        print(f"[memory] Shared {len(items_to_share)} items to shared.memories")
    except Exception as e:
        print(f"[memory] Cross-pollination failed: {e}")


# ============================================================
# STATUS — memory stats
# ============================================================

def status(agent: str) -> str:
    """Get memory stats for an agent."""
    setup_env(agent)
    from db_adapter import get_db

    db = get_db()
    display_name = AGENT_DISPLAY_NAMES.get(agent, agent)

    try:
        stats = db.get_stats()
    except Exception as e:
        return f"{display_name}: DB unavailable ({e})"

    lines = [
        f"=== Memory Status: {display_name} ({stats['schema']}) ===",
        f"  Total memories: {stats['total']}",
        f"    Core: {stats['core']}",
        f"    Active: {stats['active']}",
        f"    Archive: {stats['archive']}",
        f"  Embeddings: {stats['embeddings']}",
        f"  Graph edges: {stats['edges']}",
        f"  Sessions: {stats['sessions']}",
        f"  Last memory: {stats['last_memory'] or 'never'}",
    ]

    # Shared memory stats
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM shared.memories")
                shared_total = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM shared.memories WHERE created_by = %s",
                    (agent,)
                )
                shared_mine = cur.fetchone()[0]
        lines.append(f"  Shared memories: {shared_total} total ({shared_mine} from {agent})")
    except Exception:
        pass

    return '\n'.join(lines)


# ============================================================
# SEARCH — semantic search
# ============================================================

def search(agent: str, query: str) -> str:
    """Search agent's memories for a query."""
    setup_env(agent)
    from db_adapter import get_db, db_to_file_metadata

    db = get_db()
    display_name = AGENT_DISPLAY_NAMES.get(agent, agent)

    results = []

    # Try semantic search first
    try:
        from semantic_search import get_embedding
        embedding = get_embedding(query)
        if embedding:
            rows = db.search_similar(embedding, limit=5)
            for row in rows:
                meta, content = db_to_file_metadata(row)
                distance = row.get('distance', 0)
                similarity = 1 - distance if distance else 0
                results.append((similarity, meta, content))
    except Exception as e:
        print(f"Semantic search failed: {e}", file=sys.stderr)

    # Fallback/supplement with fulltext search
    try:
        ft_rows = db.search_fulltext(query, limit=5)
        for row in ft_rows:
            meta, content = db_to_file_metadata(row)
            # Don't duplicate semantic results
            if not any(r[1]['id'] == meta['id'] for r in results):
                results.append((row.get('rank', 0), meta, content))
    except Exception:
        pass

    if not results:
        return f"No memories found for '{query}' in {display_name}'s memory"

    # Sort by score
    results.sort(key=lambda x: x[0], reverse=True)

    lines = [f"=== Search results for '{query}' ({display_name}) ==="]
    for score, meta, content in results[:8]:
        preview = content[:200].replace('\n', ' ')
        tags = ', '.join(meta.get('tags', [])[:3])
        created = str(meta.get('created', ''))[:10]
        lines.append(f"  [{score:.2f}] ({created}) {preview}")
        if tags:
            lines.append(f"         tags: {tags}")

    return '\n'.join(lines)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Memory Wrapper for drift-agents')
    parser.add_argument('command', choices=['wake', 'sleep', 'status', 'search'],
                        help='Command to run')
    parser.add_argument('agent', choices=list(AGENT_SCHEMAS.keys()),
                        help='Agent name')
    parser.add_argument('extra', nargs='?', help='Transcript path (sleep) or query (search)')

    args = parser.parse_args()

    if args.command == 'wake':
        print(wake(args.agent))

    elif args.command == 'sleep':
        if not args.extra:
            print("ERROR: sleep requires transcript path", file=sys.stderr)
            sys.exit(1)
        success = sleep(args.agent, args.extra)
        sys.exit(0 if success else 1)

    elif args.command == 'status':
        print(status(args.agent))

    elif args.command == 'search':
        if not args.extra:
            print("ERROR: search requires a query", file=sys.stderr)
            sys.exit(1)
        print(search(args.agent, args.extra))


if __name__ == '__main__':
    main()
