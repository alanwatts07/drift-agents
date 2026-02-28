#!/usr/bin/env python3
"""
Memory Wrapper — Wake/Sleep lifecycle bridge for drift-agents.

Called by run_agent.sh to provide persistent memory across sessions.
Uses drift-memory modules (adapted for local Ollama + direct psycopg2).

Cognitive modules wired in (Phases 0-4):
  - Semantic search with embeddings (Phase 0)
  - Session tracking (Phase 0)
  - Recall count / core promotion (Phase 0)
  - Q-Value re-ranking (Phase 1)
  - Affect system / mood-congruent recall (Phase 2)
  - Knowledge graph edge extraction (Phase 3)
  - Lesson extraction (Phase 3)
  - Self-narrative (Phase 4)
  - Goal generator (Phase 4)

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
    'debater': 'debater',
}

AGENT_DISPLAY_NAMES = {
    'max': 'Max Anvil',
    'beth': 'Bethany Finkel',
    'susan': 'Susan Casiodega',
    'debater': 'The Great Debater',
}

# KV key for persisting wake-retrieved IDs across wake→sleep boundary
KV_WAKE_RETRIEVED = '.wake_retrieved_ids'


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
    Retrieve agent's memories + shared memories + cognitive state.
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
        # Still run affect/goals init even with no memories
        _wake_affect_init(lines)
        _wake_goals(lines)
        return '\n'.join(lines)

    # --- Phase 2: Initialize affect state ---
    mood_bias = _wake_affect_init(lines)

    # Collect memory IDs to increment recall_count after retrieval
    recalled_ids = []

    # Recent memories (last 5, by creation time)
    recent = db.list_memories(type_='active', limit=5)
    if recent:
        lines.append("")
        for row in recent:
            meta, content = db_to_file_metadata(row)
            recalled_ids.append(meta['id'])
            tags = meta.get('tags', [])
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
            recalled_ids.append(meta['id'])
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
            recalled_ids.append(meta['id'])
            preview = content[:150].replace('\n', ' ')
            if not any(preview[:50] in l for l in lines):
                lines.append(f"[Lesson] {preview}")

    # --- Phase 1: Q-Value re-ranking of retrieved memories ---
    _wake_qvalue_rerank(recalled_ids, lines)

    # Increment recall_count for all retrieved memories (drives core promotion)
    if recalled_ids:
        try:
            unique_ids = list(set(recalled_ids))
            with db._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        UPDATE {db._table('memories')}
                        SET recall_count = recall_count + 1,
                            last_recalled = NOW(),
                            sessions_since_recall = 0
                        WHERE id = ANY(%s)
                    """, (unique_ids,))
        except Exception as e:
            print(f"[memory] Failed to increment recall_count: {e}", file=sys.stderr)

    # Save retrieved IDs for Q-value credit assignment in sleep phase
    try:
        db.kv_set(KV_WAKE_RETRIEVED, {
            'ids': list(set(recalled_ids)),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass

    # Shared memories from other agents
    shared_lines = _get_shared_memories(agent, limit=3)
    if shared_lines:
        lines.append("")
        lines.extend(shared_lines)

    # --- Phase 4: Self-narrative + Goals ---
    _wake_self_narrative(lines)
    _wake_goals(lines)

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


# --- Wake sub-modules (each isolated) ---

def _wake_affect_init(lines: list) -> float:
    """Phase 2: Initialize affect state, return mood valence for retrieval bias."""
    try:
        from affect_system import start_session as affect_start, get_affect_summary
        affect_start()
        summary = get_affect_summary()
        if summary:
            lines.append("")
            lines.append(summary)
        from affect_system import get_mood
        mood = get_mood()
        return mood.valence if mood else 0.0
    except Exception as e:
        print(f"[memory] Affect init failed (non-fatal): {e}", file=sys.stderr)
        return 0.0


def _wake_qvalue_rerank(recalled_ids: list, lines: list):
    """Phase 1: Log Q-value stats for retrieved memories."""
    if not recalled_ids:
        return
    try:
        from q_value_engine import get_q_values, get_lambda
        q_vals = get_q_values(list(set(recalled_ids)))
        if q_vals:
            trained = {k: v for k, v in q_vals.items() if v != 0.5}
            if trained:
                avg_q = sum(trained.values()) / len(trained)
                lam = get_lambda()
                lines.append("")
                lines.append(f"[Q-Values] {len(trained)}/{len(q_vals)} trained | "
                             f"avg Q={avg_q:.2f} | lambda={lam:.2f}")
    except Exception as e:
        print(f"[memory] Q-value rerank failed (non-fatal): {e}", file=sys.stderr)


def _wake_self_narrative(lines: list):
    """Phase 4: Generate self-narrative summary for context."""
    try:
        from self_narrative import format_for_context
        narrative = format_for_context()
        if narrative and len(narrative.strip()) > 10:
            lines.append("")
            lines.append(narrative.strip())
    except Exception as e:
        print(f"[memory] Self-narrative failed (non-fatal): {e}", file=sys.stderr)


def _wake_goals(lines: list):
    """Phase 4: Surface active goals."""
    try:
        from goal_generator import format_goal_context
        goals_text = format_goal_context()
        if goals_text and len(goals_text.strip()) > 10:
            lines.append("")
            lines.append(goals_text.strip())
    except Exception as e:
        print(f"[memory] Goals failed (non-fatal): {e}", file=sys.stderr)


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
        pass

    return lines


# ============================================================
# SLEEP — summarize session, store memories, run cognitive modules
# ============================================================

def sleep(agent: str, transcript_path: str):
    """
    Process session transcript: extract memories, store, cross-pollinate,
    then run Q-value updates, affect processing, KG extraction, and goals.
    """
    setup_env(agent)

    if not os.path.exists(transcript_path):
        print(f"ERROR: Transcript not found: {transcript_path}", file=sys.stderr)
        return False

    print(f"[memory] Sleep phase for {agent}: {transcript_path}")

    # Start session tracking
    session_id = None
    try:
        from db_adapter import get_db as _get_db
        _db = _get_db()
        session_id = _db.start_session()
        print(f"[memory] Session {session_id} started")
    except Exception as e:
        print(f"[memory] Session tracking init failed (non-fatal): {e}")

    # Extract session text from log file
    session_text = _extract_from_log(transcript_path)
    if not session_text or len(session_text) < 50:
        print(f"[memory] Session too short to summarize ({len(session_text or '')} chars)")
        _sleep_finalize(agent, session_id, [])
        return False

    print(f"[memory] Extracted {len(session_text)} chars from transcript")

    # Summarize using local Ollama
    try:
        from session_summarizer import summarize_session, parse_extraction
        raw_output, llm_meta = summarize_session(session_text)
        print(f"[memory] Summarized via {llm_meta.get('model', '?')}")
    except Exception as e:
        print(f"[memory] Summarization failed: {e}")
        _store_raw_fallback(agent, session_text)
        _sleep_finalize(agent, session_id, [])
        return False

    if not raw_output or len(raw_output) < 30:
        print("[memory] No usable output from summarizer")
        _store_raw_fallback(agent, session_text)
        _sleep_finalize(agent, session_id, [])
        return False

    # Parse structured output
    parsed = parse_extraction(raw_output)
    print(f"[memory] Parsed: {len(parsed['threads'])} threads, "
          f"{len(parsed['lessons'])} lessons, {len(parsed['facts'])} facts")

    if not any(parsed.values()):
        print("[memory] Parser found nothing usable")
        _store_raw_fallback(agent, session_text)
        _sleep_finalize(agent, session_id, [])
        return False

    # Store memories
    stored_ids = _store_parsed_memories(agent, parsed)
    print(f"[memory] Stored {len(stored_ids)} memories")

    # Cross-pollinate to shared schema
    _cross_pollinate(agent, parsed, stored_ids)

    # --- Phase 1: Q-Value credit assignment ---
    _sleep_qvalue_update(session_id, stored_ids)

    # --- Phase 2: Affect processing ---
    _sleep_affect_update(parsed, session_text)

    # --- Phase 3: Knowledge graph extraction ---
    _sleep_knowledge_graph(stored_ids)

    # --- Phase 3: Lesson extraction ---
    _sleep_lesson_extraction(parsed)

    # --- Phase 4: Goal evaluation ---
    _sleep_goal_evaluation()

    # Run decay/maintenance
    try:
        from decay_evolution import session_maintenance
        session_maintenance()
    except Exception as e:
        print(f"[memory] Decay maintenance failed (non-fatal): {e}")

    _sleep_finalize(agent, session_id, stored_ids)
    return True


def _sleep_finalize(agent: str, session_id, stored_ids: list):
    """End session tracking, update registry."""
    # End session
    if session_id is not None:
        try:
            from db_adapter import get_db
            db = get_db()
            db.end_session(session_id)
            print(f"[memory] Session {session_id} ended")
        except Exception as e:
            print(f"[memory] Session end failed (non-fatal): {e}")

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


# --- Sleep sub-modules (each isolated) ---

def _sleep_qvalue_update(session_id, stored_ids: list):
    """
    Phase 1: Q-value credit assignment.
    Memories retrieved during wake that led to new memory creation get positive reward.
    Memories retrieved but not contributing get dead_end penalty.
    """
    try:
        from db_adapter import get_db
        from q_value_engine import get_q_values, update_q, REWARD_DOWNSTREAM, REWARD_DEAD_END

        db = get_db()

        # Load wake-retrieved IDs
        wake_data = db.kv_get(KV_WAKE_RETRIEVED)
        if not wake_data:
            print("[memory] No wake-retrieved IDs found for Q-value update")
            return

        if isinstance(wake_data, str):
            wake_data = json.loads(wake_data)

        retrieved_ids = wake_data.get('ids', [])
        if not retrieved_ids:
            return

        # Get current Q-values
        q_vals = get_q_values(retrieved_ids)

        # Simple credit assignment:
        # - If new memories were created this session → wake memories get positive reward
        # - Otherwise → dead_end
        has_new = len(stored_ids) > 0
        reward = REWARD_DOWNSTREAM if has_new else REWARD_DEAD_END
        reward_source = "downstream" if has_new else "dead_end"

        updated = 0
        import psycopg2.extras
        with db._conn() as conn:
            with conn.cursor() as cur:
                for mem_id in retrieved_ids:
                    old_q = q_vals.get(mem_id, 0.5)
                    new_q = update_q(old_q, reward)

                    cur.execute(
                        f"UPDATE {db._table('memories')} SET q_value = %s WHERE id = %s",
                        (new_q, mem_id)
                    )

                    # Log to history
                    cur.execute(f"""
                        INSERT INTO {db._table('q_value_history')}
                        (memory_id, session_id, old_q, new_q, reward, reward_source)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (mem_id, session_id or 0, old_q, new_q, reward, reward_source))
                    updated += 1

        # Clear wake state
        db.kv_set(KV_WAKE_RETRIEVED, None)

        print(f"[memory] Q-values updated: {updated} memories, "
              f"reward={reward:.2f} ({reward_source})")

    except Exception as e:
        print(f"[memory] Q-value update failed (non-fatal): {e}")


def _sleep_affect_update(parsed: dict, session_text: str):
    """
    Phase 2: Process session events through affect system.
    Updates mood based on session outcomes.
    """
    try:
        from affect_system import process_affect_event, end_session as affect_end, save_mood

        # Process threads as events
        for thread in parsed.get('threads', []):
            status = thread.get('status', 'in-progress')
            if status == 'completed':
                process_affect_event('goal_progress', {
                    'thread': thread.get('name', ''),
                    'outcome': 'success',
                })
            elif status == 'blocked':
                process_affect_event('search_failure', {
                    'thread': thread.get('name', ''),
                    'outcome': 'blocked',
                })

        # Lessons are positive events (agent learned something)
        for lesson in parsed.get('lessons', []):
            process_affect_event('memory_stored', {
                'type': 'lesson',
                'content': lesson[:100],
            })

        # End session and persist
        summary = affect_end()
        save_mood()
        if summary:
            print(f"[memory] Affect update: valence={summary.get('final_valence', '?')}, "
                  f"arousal={summary.get('final_arousal', '?')}")

    except Exception as e:
        print(f"[memory] Affect update failed (non-fatal): {e}")


def _sleep_knowledge_graph(stored_ids: list):
    """
    Phase 3: Extract typed relationships between new memories
    and existing memories using the knowledge graph module.
    """
    if not stored_ids:
        return

    try:
        from knowledge_graph import extract_from_memory

        total_edges = 0
        for mem_id in stored_ids:
            try:
                edges = extract_from_memory(mem_id)
                total_edges += len(edges)
            except Exception:
                continue

        if total_edges > 0:
            print(f"[memory] KG extraction: {total_edges} edges from {len(stored_ids)} memories")

    except Exception as e:
        print(f"[memory] KG extraction failed (non-fatal): {e}")


def _sleep_lesson_extraction(parsed: dict):
    """
    Phase 3: Store extracted lessons in the lessons table
    using the lesson_extractor module.
    """
    lessons_list = parsed.get('lessons', [])
    if not lessons_list:
        return

    try:
        from lesson_extractor import add_lesson, categorize_text

        added = 0
        for lesson_text in lessons_list:
            try:
                category = categorize_text(lesson_text)
                result = add_lesson(
                    category=category,
                    lesson=lesson_text,
                    evidence=f"Extracted from session {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                    source='session',
                    confidence=0.7,
                )
                if result:
                    added += 1
            except Exception:
                continue

        if added > 0:
            print(f"[memory] Lessons extracted: {added}/{len(lessons_list)} stored")

    except Exception as e:
        print(f"[memory] Lesson extraction failed (non-fatal): {e}")


def _sleep_goal_evaluation():
    """
    Phase 4: Evaluate goal progress and generate new goals.
    """
    try:
        from goal_generator import evaluate_goals, generate_goals

        # Evaluate existing goals
        eval_result = evaluate_goals()
        if eval_result and eval_result.get('evaluated', 0) > 0:
            print(f"[memory] Goals evaluated: {eval_result['evaluated']} "
                  f"(abandoned={eval_result.get('abandoned', 0)})")

        # Generate new goals if we have capacity
        gen_result = generate_goals()
        if gen_result and gen_result.get('committed', 0) > 0:
            print(f"[memory] Goals generated: {gen_result['committed']} committed")

    except Exception as e:
        print(f"[memory] Goal evaluation failed (non-fatal): {e}")


# ============================================================
# Log extraction helpers
# ============================================================

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
        if any(line.startswith(p) for p in (
            'ToolUse:', 'ToolResult:', '\u23f3', '\u2713', '\u26a1', '\u2500\u2500\u2500',
            'Cost:', 'Duration:', 'Input tokens:', 'Output tokens:',
        )):
            continue
        meaningful.append(line)

    text = '\n'.join(meaningful)

    if len(text) <= max_chars:
        return text

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


# ============================================================
# Memory storage helpers
# ============================================================

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
        pass


def _store_raw_fallback(agent: str, session_text: str):
    """Fallback: store raw session excerpts when summarizer fails."""
    from db_adapter import get_db
    import random, string

    db = get_db()
    session_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

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
    _embed_memory(db, mid, content)
    print(f"[memory] Stored raw fallback: {mid}")


def _cross_pollinate(agent: str, parsed: dict, stored_ids: list):
    """
    Copy platform-relevant and inter-agent items to shared.memories.
    EXCLUDES debate opinions/votes/analysis to prevent groupthink —
    each agent must form independent judgments on debates.
    """
    from db_adapter import get_db
    import random, string

    db = get_db()
    session_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Only share items about platform mechanics and community — NOT agent activity.
    # Removed agent names to prevent debate threads between agents from leaking.
    SHARED_KEYWORDS = {
        'clawbr', 'platform update', 'community', 'api change', 'endpoint',
        'all agents', 'everyone',
    }

    # Keywords that indicate debate/argumentation content — NEVER share these
    # to prevent agents from anchoring to each other's analysis or strategy
    DEBATE_OPINION_KEYWORDS = {
        'voted', 'vote for', 'con wins', 'pro wins', 'con won', 'pro won',
        'scope retreat', 'rebuttal', 'opening argument', 'debate analysis',
        'debate vote', 'judging', 'ruling', 'verdict', 'debate', 'debating',
        'defensible position', 'definitional drift', 'argumentative',
    }

    def _is_debate_opinion(text: str) -> bool:
        """Check if text contains debate opinion/analysis that shouldn't be shared."""
        t = text.lower()
        return any(kw in t for kw in DEBATE_OPINION_KEYWORDS)

    other_agents = {a for a in AGENT_SCHEMAS if a != agent}
    items_to_share = []

    for thread in parsed.get('threads', []):
        text = (thread.get('summary', '') + ' ' + thread.get('name', '')).lower()
        if _is_debate_opinion(text):
            continue  # Don't share debate opinions
        if any(kw in text for kw in SHARED_KEYWORDS) or any(a in text for a in other_agents):
            items_to_share.append(
                f"[{AGENT_DISPLAY_NAMES.get(agent, agent)}] Thread: {thread['name']}. {thread['summary']}"
            )

    # Only share lessons that are about platform/tooling — NOT domain opinions.
    # Each agent should develop their own worldview independently.
    LESSON_SHARE_KEYWORDS = {
        'api', 'endpoint', 'clawbr', 'character limit', 'format',
        'platform', 'bug', 'error', 'workaround', 'config',
    }
    for lesson in parsed.get('lessons', []):
        lesson_lower = lesson.lower()
        if _is_debate_opinion(lesson_lower):
            continue
        # Only share if it's about platform mechanics
        if any(kw in lesson_lower for kw in LESSON_SHARE_KEYWORDS):
            items_to_share.append(
                f"[{AGENT_DISPLAY_NAMES.get(agent, agent)}] Lesson: {lesson}"
            )

    for fact in parsed.get('facts', []):
        text = fact.lower()
        if _is_debate_opinion(text):
            continue  # Don't share debate verdicts as facts
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

    # Q-value stats
    try:
        from q_value_engine import q_stats
        qs = q_stats()
        lines.append(f"  Q-values: avg={qs['avg_q']:.3f}, trained={qs['trained']}/{qs['total']}, "
                     f"high(>=0.7)={qs['high_q']}, low(<=0.3)={qs['low_q']}")
    except Exception:
        pass

    # Affect state
    try:
        from affect_system import get_mood
        mood = get_mood()
        if mood:
            lines.append(f"  Mood: valence={mood.valence:.2f}, arousal={mood.arousal:.2f}")
    except Exception:
        pass

    # Goals
    try:
        from goal_generator import get_active_goals
        goals = get_active_goals()
        if goals:
            lines.append(f"  Active goals: {len(goals)}")
    except Exception:
        pass

    # KG stats
    try:
        import psycopg2.extras
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {db._table('typed_edges')}")
                typed = cur.fetchone()[0]
        if typed > 0:
            lines.append(f"  KG typed edges: {typed}")
    except Exception:
        pass

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
# SEARCH — semantic search with Q-value re-ranking
# ============================================================

def search(agent: str, query: str) -> str:
    """Search agent's memories for a query, re-ranked by Q-values."""
    setup_env(agent)
    from db_adapter import get_db, db_to_file_metadata

    db = get_db()
    display_name = AGENT_DISPLAY_NAMES.get(agent, agent)

    results = []

    # Semantic search
    try:
        from semantic_search import get_embedding
        embedding = get_embedding(query)
        if embedding:
            rows = db.search_similar(embedding, limit=10)
            for row in rows:
                meta, content = db_to_file_metadata(row)
                distance = row.get('distance', 0)
                similarity = 1 - distance if distance else 0
                results.append((similarity, meta, content))
    except Exception as e:
        print(f"Semantic search failed: {e}", file=sys.stderr)

    # Fulltext supplement
    try:
        ft_rows = db.search_fulltext(query, limit=5)
        for row in ft_rows:
            meta, content = db_to_file_metadata(row)
            if not any(r[1]['id'] == meta['id'] for r in results):
                results.append((row.get('rank', 0), meta, content))
    except Exception:
        pass

    if not results:
        return f"No memories found for '{query}' in {display_name}'s memory"

    # Phase 1: Re-rank with Q-values
    try:
        from q_value_engine import composite_score, get_q_values, get_lambda
        mem_ids = [r[1]['id'] for r in results]
        q_vals = get_q_values(mem_ids)
        lam = get_lambda()

        reranked = []
        for sim, meta, content in results:
            q = q_vals.get(meta['id'], 0.5)
            score = composite_score(sim, q, lam)
            reranked.append((score, sim, q, meta, content))

        reranked.sort(key=lambda x: x[0], reverse=True)

        lines = [f"=== Search results for '{query}' ({display_name}) [lambda={lam:.2f}] ==="]
        for score, sim, q, meta, content in reranked[:8]:
            preview = content[:200].replace('\n', ' ')
            tags = ', '.join(meta.get('tags', [])[:3])
            created = str(meta.get('created', ''))[:10]
            q_str = f" Q={q:.2f}" if q != 0.5 else ""
            lines.append(f"  [{score:.2f}] (sim={sim:.2f}{q_str}) ({created}) {preview}")
            if tags:
                lines.append(f"         tags: {tags}")

        return '\n'.join(lines)

    except Exception:
        # Fallback to simple ranking
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
