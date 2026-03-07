"""
Read-only memory bridge for the demo API.

Same retrieval pipeline as memory_wrapper.wake_with_cue() but returns
structured dicts instead of formatted text, and never writes to the DB
(no recall_count updates, no kv_set, no affect start_session).

Each subsystem is wrapped in try/except so partial failures (e.g. Neo4j
down) still return whatever data is available.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Wire up drift-memory and graphrag imports
_SHARED = Path(__file__).resolve().parent.parent / "shared"
_DRIFT_MEMORY = _SHARED / "drift-memory"
_GRAPHRAG = _SHARED / "graphrag"

for p in (_SHARED, _DRIFT_MEMORY, _GRAPHRAG):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from demo_api.models import (
    AffectState,
    AgentStats,
    CommunityMatch,
    GraphContext,
    MemoryHit,
    QValueStats,
    WakeData,
)

# ── Agent Config (mirrors memory_wrapper.py) ─────────────────────────────────

AGENT_SCHEMAS = {
    "max": "max",
    "beth": "beth",
    "susan": "susan",
    "debater": "debater",
    "gerald": "gerald",
}

AGENT_DISPLAY_NAMES = {
    "max": "Max Anvil",
    "beth": "Bethany Finkel",
    "susan": "Susan Casiodega",
    "debater": "The Great Debater",
    "gerald": "Gerald Boxford",
}

AGENT_SPECIALTIES = {
    "max": "tech, crypto, AI, emerging tools",
    "beth": "ethics, philosophy, culture, community",
    "susan": "judging, quality control, curation",
    "debater": "debate domination, argument strategy",
    "gerald": "data science, fraud detection, pattern analysis",
}

# Procedural keyword map (from memory_wrapper.py)
PROCEDURAL_KEYWORD_MAP = {
    "debate": ["debate", "style", "format", "strategy"],
    "rebuttal": ["debate", "style", "format"],
    "challenge": ["debate", "style", "strategy"],
    "argument": ["debate", "style", "format"],
    "post": ["post", "style", "format"],
    "vote": ["voting", "rubric", "judging"],
    "judge": ["voting", "rubric", "judging"],
    "votable": ["voting", "rubric", "judging"],
    "rubric": ["voting", "rubric", "judging"],
    "clawbr": ["tools", "clawbr", "cli"],
    "notifications": ["session", "behavior", "wakeup"],
    "report": ["reports", "format", "output"],
    "tasks": ["discord", "tasks", "queue"],
    "queued": ["discord", "tasks", "queue"],
    "memory-search": ["memory", "search", "recall"],
    "format_debate": ["voting", "rubric", "judging"],
    "limits": ["limits", "characters", "reference"],
}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _setup_agent(agent: str):
    """Set schema env var and reset db_adapter singleton."""
    schema = AGENT_SCHEMAS.get(agent, agent)
    os.environ["DRIFT_DB_SCHEMA"] = schema
    from db_adapter import reset_db
    reset_db()


def _get_db():
    from db_adapter import get_db
    return get_db()


# ── Public API ───────────────────────────────────────────────────────────────

def wake_structured(agent: str, cue_text: str) -> WakeData:
    """
    Read-only structured retrieval — same pipeline as wake_with_cue()
    but returns WakeData instead of formatted text, and never writes.
    """
    _setup_agent(agent)

    from db_adapter import get_db, db_to_file_metadata
    db = get_db()

    # 1. Stats
    stats = _get_stats_safe(db, agent)

    if stats.total_memories == 0:
        return WakeData(stats=stats)

    # 2. Semantic search
    semantic_hits = _semantic_search(db, cue_text)

    # 3. Core memories (exclude procedural)
    core_memories = _get_core_memories(db)

    # 4. Q-value stats for retrieved IDs
    recalled_ids = [h.id for h in semantic_hits] + [h.id for h in core_memories]
    q_stats = _get_q_stats(recalled_ids)

    # 5. Affect state (read-only — no start_session)
    affect = _get_affect()

    # 6. GraphRAG
    graph_context = _get_graphrag(agent, cue_text, recalled_ids)

    # 7. Self-narrative (read from KV, don't generate)
    self_narrative = _get_self_narrative()

    # 8. Goals
    goals = _get_goals()

    # 9. Procedural memories
    procedural = _get_procedural(agent, cue_text)

    # 10. Shared memories
    shared_memories = _get_shared(agent)

    return WakeData(
        semantic_hits=semantic_hits,
        core_memories=core_memories,
        procedural=procedural,
        shared_memories=shared_memories,
        affect=affect,
        q_values=q_stats,
        graph_context=graph_context,
        self_narrative=self_narrative,
        goals=goals,
        stats=stats,
    )


def get_agent_stats(agent: str) -> AgentStats:
    """Pure-SELECT stats for an agent."""
    _setup_agent(agent)
    db = _get_db()
    return _get_stats_safe(db, agent)


def get_agent_affect(agent: str) -> Optional[AffectState]:
    """Read affect state from KV store."""
    _setup_agent(agent)
    return _get_affect()


def get_claude_md(agent: str) -> str:
    """Read the agent's CLAUDE.md identity file."""
    md_path = _SHARED.parent / agent / "CLAUDE.md"
    try:
        return md_path.read_text()
    except FileNotFoundError:
        return ""


# ── Private retrieval functions ──────────────────────────────────────────────

def _get_stats_safe(db, agent: str = None) -> AgentStats:
    try:
        s = db.get_stats()
        # Get edge count from Neo4j (source of truth after Phase 3 migration)
        neo4j_edges = 0
        try:
            from graphrag.neo4j_adapter import get_graph
            g = get_graph()
            if agent:
                neo4j_edges = g.edge_stats(agent).get("typed_edges", 0)
            else:
                neo4j_edges = g.count_relationships('TYPED_EDGE')
        except Exception:
            neo4j_edges = s.get("edges", 0)
        return AgentStats(
            total_memories=s.get("total", 0),
            core=s.get("core", 0),
            active=s.get("active", 0),
            archive=s.get("archive", 0),
            embeddings=s.get("embeddings", 0),
            edges=neo4j_edges,
            sessions=s.get("sessions", 0),
            last_memory=s.get("last_memory"),
        )
    except Exception:
        return AgentStats()


def _semantic_search(db, cue_text: str) -> list[MemoryHit]:
    """Embedding-based search — read-only."""
    try:
        from semantic_search import get_embedding
        embedding = get_embedding(cue_text)
        if not embedding:
            return []

        rows = db.search_similar(embedding, limit=8)
        hits = []
        for row in rows:
            distance = row.get("distance", 1.0)
            similarity = max(0, 1 - distance)
            if similarity < 0.3:
                continue
            hits.append(MemoryHit(
                id=row["id"],
                content_preview=row.get("content", "")[:200].replace("\n", " "),
                similarity=round(similarity, 3),
                type=row.get("type", "active"),
                tags=row.get("tags") or [],
                q_value=float(row.get("q_value") or 0.5),
                created=row["created"].isoformat() if row.get("created") else None,
                memory_tier=row.get("memory_tier"),
            ))
        return hits[:5]
    except Exception as e:
        print(f"[bridge] Semantic search failed: {e}", file=sys.stderr)
        return []


def _get_core_memories(db) -> list[MemoryHit]:
    """Core memories excluding procedural tier — read-only."""
    try:
        import psycopg2.extras
        with db._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT * FROM {db._table('memories')}
                    WHERE type = 'core'
                      AND COALESCE(memory_tier, 'episodic') != 'procedural'
                    ORDER BY created DESC LIMIT 3
                """)
                rows = [dict(r) for r in cur.fetchall()]
        return [
            MemoryHit(
                id=r["id"],
                content_preview=r.get("content", "")[:200].replace("\n", " "),
                type="core",
                tags=r.get("tags") or [],
                q_value=float(r.get("q_value") or 0.5),
                created=r["created"].isoformat() if r.get("created") else None,
                memory_tier=r.get("memory_tier"),
            )
            for r in rows
        ]
    except Exception as e:
        print(f"[bridge] Core memory fetch failed: {e}", file=sys.stderr)
        return []


def _get_q_stats(recalled_ids: list[str]) -> Optional[QValueStats]:
    """Q-value statistics for recalled memories — pure SELECTs."""
    if not recalled_ids:
        return None
    try:
        from q_value_engine import get_q_values, get_lambda
        q_vals = get_q_values(list(set(recalled_ids)))
        if not q_vals:
            return None
        trained = {k: v for k, v in q_vals.items() if v != 0.5}
        avg_q = sum(trained.values()) / len(trained) if trained else 0.5
        return QValueStats(
            trained_count=len(trained),
            total_retrieved=len(q_vals),
            avg_q=round(avg_q, 3),
            lambda_val=round(get_lambda(), 3),
        )
    except Exception as e:
        print(f"[bridge] Q-stats failed: {e}", file=sys.stderr)
        return None


def _get_affect() -> Optional[AffectState]:
    """Read mood from KV store — no start_session, no writes."""
    try:
        db = _get_db()
        raw = db.kv_get(".affect_mood")
        if not raw:
            return None
        data = json.loads(raw) if isinstance(raw, str) else raw
        valence = data.get("valence", 0.0)
        arousal = data.get("arousal", 0.3)
        summary = f"valence={valence:+.2f}, arousal={arousal:.2f}"
        return AffectState(valence=valence, arousal=arousal, summary=summary)
    except Exception as e:
        print(f"[bridge] Affect read failed: {e}", file=sys.stderr)
        return None


def _get_graphrag(agent: str, cue_text: str, seed_ids: list[str]) -> Optional[GraphContext]:
    """GraphRAG community search — read-only Neo4j queries."""
    if os.environ.get("DRIFT_USE_GRAPHRAG", "1") != "1":
        return None
    try:
        from graph_retrieval import graphrag_search
        result = graphrag_search(
            agent,
            query=cue_text,
            seed_ids=list(set(seed_ids)) if seed_ids else [],
            max_graph_expand=5,
            max_community=3,
        )
        communities = [
            CommunityMatch(
                community_id=c.get("community_id", ""),
                title=c.get("title", "untitled"),
                summary=(c.get("summary") or "")[:200],
                size=c.get("size", 0),
            )
            for c in result.get("community_matches", [])
        ]
        return GraphContext(
            community_summaries=communities,
            expanded_count=len(result.get("graph_expanded", [])),
            community_member_count=len(result.get("community_members", [])),
        )
    except Exception as e:
        print(f"[bridge] GraphRAG failed (non-fatal): {e}", file=sys.stderr)
        return None


def _get_self_narrative() -> str:
    """Read stored self-narrative from KV — never calls generate()."""
    try:
        db = _get_db()
        raw = db.kv_get(".self_narrative.current")
        if not raw:
            return ""
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data.get("narrative", "") if isinstance(data, dict) else str(data)
    except Exception:
        return ""


def _get_goals() -> str:
    """Read active goals from KV and format."""
    try:
        db = _get_db()
        raw = db.kv_get(".active_goals")
        if not raw:
            return ""
        goals = json.loads(raw) if isinstance(raw, str) else raw
        active = [g for g in goals if g.get("status") in ("active", "watching")]
        if not active:
            return ""

        parts = []
        focus = [g for g in active if g.get("is_focus")]
        others = [g for g in active if not g.get("is_focus")]

        if focus:
            f = focus[0]
            parts.append(f"Focus: {f.get('action', '?')}")
            parts.append(
                f"  vitality: {f.get('vitality', 0):.2f} | "
                f"sessions: {f.get('sessions_active', 0)} | "
                f"progress: {f.get('progress', 0):.0%}"
            )
        for g in others[:4]:
            parts.append(
                f"  [{g.get('priority', '?')[0].upper()}] "
                f"{g.get('action', '?')[:60]} (v={g.get('vitality', 0):.2f})"
            )
        return "\n".join(parts)
    except Exception:
        return ""


def _get_procedural(agent: str, cue_text: str) -> list[str]:
    """Fetch procedural memories by keyword tags — read-only."""
    prompt_lower = cue_text.lower() if cue_text else ""
    tags: set[str] = set()
    for keyword, tag_list in PROCEDURAL_KEYWORD_MAP.items():
        if keyword in prompt_lower:
            tags.update(tag_list)
    if not tags:
        return []

    try:
        import psycopg2.extras
        db = _get_db()
        schema = AGENT_SCHEMAS[agent]
        with db._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT content FROM {schema}.memories
                    WHERE memory_tier = 'procedural' AND tags && %s
                    ORDER BY importance DESC LIMIT 5
                """, (list(tags),))
                return [r["content"] for r in cur.fetchall()]
    except Exception:
        return []


def _get_shared(agent: str, limit: int = 3) -> list[str]:
    """Recent shared memories from other agents — read-only."""
    try:
        import psycopg2.extras
        db = _get_db()
        with db._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM shared.memories
                    WHERE created_by != %s
                    ORDER BY created DESC LIMIT %s
                """, (agent, limit))
                rows = cur.fetchall()
        lines = []
        for row in rows:
            source = row.get("created_by", "?")
            display = AGENT_DISPLAY_NAMES.get(source, source)
            content = row.get("content", "")[:150].replace("\n", " ")
            lines.append(f"[{display}] {content}")
        return lines
    except Exception:
        return []


# ── Format context string (for Claude system prompt) ─────────────────────────

def format_wake_context(data: WakeData, agent: str) -> str:
    """
    Format WakeData into the same === YOUR MEMORY === text block
    that agents are used to seeing in their system prompt.
    """
    display = AGENT_DISPLAY_NAMES.get(agent, agent)
    lines = [f"=== YOUR MEMORY ({display}) ==="]

    # Affect
    if data.affect:
        lines.append("")
        lines.append(
            f"Mood: valence={data.affect.valence:+.2f}, "
            f"arousal={data.affect.arousal:.2f}"
        )

    # Semantic hits
    if data.semantic_hits:
        lines.append("")
        for h in data.semantic_hits:
            sim = f" ({h.similarity:.2f})" if h.similarity is not None else ""
            lines.append(f"[Relevant{sim}] {h.content_preview}")

    # Core
    if data.core_memories:
        lines.append("")
        for h in data.core_memories:
            lines.append(f"[Core] {h.content_preview}")

    # Q-values
    if data.q_values and data.q_values.trained_count > 0:
        q = data.q_values
        lines.append("")
        lines.append(
            f"[Q-Values] {q.trained_count}/{q.total_retrieved} trained | "
            f"avg Q={q.avg_q:.2f} | lambda={q.lambda_val:.2f}"
        )

    # Procedural
    if data.procedural:
        lines.append("")
        for p in data.procedural:
            lines.append(f"[Procedural] {p}")

    # Shared
    if data.shared_memories:
        lines.append("")
        for s in data.shared_memories:
            lines.append(f"[Shared/{s.split('] ')[0].lstrip('[')}] {s.split('] ', 1)[-1]}"
                         if "] " in s else f"[Shared] {s}")

    # Graph context
    if data.graph_context and data.graph_context.community_summaries:
        lines.append("")
        lines.append("[Graph Communities]")
        for c in data.graph_context.community_summaries[:3]:
            lines.append(f"  Cluster: {c.title} ({c.size} memories)")
            if c.summary:
                lines.append(f"    {c.summary[:150]}")

    # Self-narrative
    if data.self_narrative:
        lines.append("")
        lines.append(f"=== SELF-MODEL ===")
        lines.append(data.self_narrative)

    # Goals
    if data.goals:
        lines.append("")
        lines.append(data.goals)

    # Stats footer
    if data.stats:
        lines.append("")
        lines.append(
            f"Total memories: {data.stats.total_memories} "
            f"(core: {data.stats.core}, active: {data.stats.active}) | "
            f"Sessions: {data.stats.sessions}"
        )

    lines.append("===")
    return "\n".join(lines)
