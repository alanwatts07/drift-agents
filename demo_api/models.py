"""Pydantic models for the drift-agents demo API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Request Models ───────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    agent: str
    message: str
    history: list[ChatMessage] = Field(default_factory=list)


# ── Memory Hit (shared shape for semantic + core) ───────────────────────────

class MemoryHit(BaseModel):
    id: str
    content_preview: str
    similarity: Optional[float] = None
    type: str  # core / active / archive
    tags: list[str] = Field(default_factory=list)
    q_value: float = 0.5
    created: Optional[str] = None
    memory_tier: Optional[str] = None


# ── Affect State ─────────────────────────────────────────────────────────────

class AffectState(BaseModel):
    valence: float = 0.0
    arousal: float = 0.3
    summary: str = ""


# ── Q-Value Stats ────────────────────────────────────────────────────────────

class QValueStats(BaseModel):
    trained_count: int = 0
    total_retrieved: int = 0
    avg_q: float = 0.5
    lambda_val: float = 0.5  # "lambda" is reserved


# ── Graph Context ────────────────────────────────────────────────────────────

class CommunityMatch(BaseModel):
    community_id: str
    title: str
    summary: str = ""
    size: int = 0


class GraphExpanded(BaseModel):
    id: str
    content: str = ""
    rel_type: str = ""
    importance: float = 0.5

class CommunityMember(BaseModel):
    id: str
    content: str = ""
    community_title: str = ""

class GraphContext(BaseModel):
    community_summaries: list[CommunityMatch] = Field(default_factory=list)
    expanded: list[GraphExpanded] = Field(default_factory=list)
    community_members: list[CommunityMember] = Field(default_factory=list)
    expanded_count: int = 0
    community_member_count: int = 0


# ── Wake Data (the memory panel payload) ─────────────────────────────────────

class WakeData(BaseModel):
    semantic_hits: list[MemoryHit] = Field(default_factory=list)
    core_memories: list[MemoryHit] = Field(default_factory=list)
    procedural: list[str] = Field(default_factory=list)
    shared_memories: list[str] = Field(default_factory=list)
    affect: Optional[AffectState] = None
    q_values: Optional[QValueStats] = None
    graph_context: Optional[GraphContext] = None
    self_narrative: str = ""
    goals: str = ""
    stats: Optional[AgentStats] = None


# ── Agent Stats ──────────────────────────────────────────────────────────────

class AgentStats(BaseModel):
    total_memories: int = 0
    core: int = 0
    active: int = 0
    archive: int = 0
    embeddings: int = 0
    edges: int = 0
    sessions: int = 0
    last_memory: Optional[str] = None


# Rebuild WakeData to resolve forward ref
WakeData.model_rebuild()


# ── Response Models ──────────────────────────────────────────────────────────

class ChatResponse(BaseModel):
    response: str
    memories_used: WakeData
    agent: str
    agent_display: str


class AgentInfo(BaseModel):
    name: str
    display_name: str
    specialty: str
    stats: AgentStats


class AgentListResponse(BaseModel):
    agents: list[AgentInfo]


class AgentStatusResponse(BaseModel):
    name: str
    display_name: str
    specialty: str
    stats: AgentStats
    affect: Optional[AffectState] = None
    goals: str = ""
    self_narrative: str = ""
