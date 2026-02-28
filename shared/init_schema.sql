-- Drift-Agents Memory Schema
-- Per-agent schemas (max, beth, susan) + shared cross-agent schema
-- Based on drift-memory by DriftCornwall (MIT License)
-- Date: 2026-02-22

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- SCHEMA TEMPLATE FUNCTION
-- Creates identical table structure for any agent
-- Usage: SELECT create_agent_schema('max');
-- ============================================================

CREATE OR REPLACE FUNCTION create_agent_schema(agent_name TEXT)
RETURNS void AS $$
BEGIN
    -- Create schema
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', agent_name);

    -- 1. MEMORIES — core table
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.memories (
            id TEXT PRIMARY KEY,
            type VARCHAR(10) NOT NULL CHECK (type IN (''core'', ''active'', ''archive'')),
            content TEXT NOT NULL,
            created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_recalled TIMESTAMPTZ,
            recall_count INTEGER DEFAULT 0,
            sessions_since_recall INTEGER DEFAULT 0,
            emotional_weight FLOAT DEFAULT 0.5,
            tags TEXT[],
            event_time TIMESTAMPTZ,
            entities JSONB DEFAULT ''{}''::jsonb,
            caused_by TEXT[],
            leads_to TEXT[],
            source JSONB,
            retrieval_outcomes JSONB DEFAULT ''{"productive":0,"generative":0,"dead_end":0,"total":0}''::jsonb,
            retrieval_success_rate FLOAT,
            topic_context TEXT[],
            contact_context TEXT[],
            platform_context TEXT[],
            extra_metadata JSONB DEFAULT ''{}''::jsonb,
            importance DOUBLE PRECISION DEFAULT 0.5,
            freshness DOUBLE PRECISION DEFAULT 1.0,
            memory_tier VARCHAR(20) DEFAULT ''episodic'',
            valence FLOAT DEFAULT 0.0,
            q_value FLOAT DEFAULT 0.5
        )', agent_name);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_memories_type ON %I.memories(type)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_memories_tags ON %I.memories USING GIN(tags)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_memories_created ON %I.memories(created)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_memories_last_recalled ON %I.memories(last_recalled)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_memories_emotional_weight ON %I.memories(emotional_weight)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_memories_entities ON %I.memories USING GIN(entities)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_memories_topic ON %I.memories USING GIN(topic_context)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_memories_fts ON %I.memories USING GIN(to_tsvector(''english'', content))', agent_name, agent_name);

    -- 2. CO-OCCURRENCES
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.co_occurrences (
            memory_id TEXT NOT NULL,
            other_id TEXT NOT NULL,
            count FLOAT DEFAULT 0,
            PRIMARY KEY (memory_id, other_id)
        )', agent_name);

    -- 3. EDGES V3 — provenance-based co-occurrence graph
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.edges_v3 (
            id1 TEXT NOT NULL,
            id2 TEXT NOT NULL,
            belief FLOAT DEFAULT 0,
            first_formed TIMESTAMPTZ,
            last_updated TIMESTAMPTZ,
            platform_context JSONB DEFAULT ''{}''::jsonb,
            activity_context JSONB DEFAULT ''{}''::jsonb,
            topic_context JSONB DEFAULT ''{}''::jsonb,
            contact_context TEXT[],
            thinking_about TEXT[],
            PRIMARY KEY (id1, id2),
            CHECK (id1 < id2)
        )', agent_name);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_edges_belief ON %I.edges_v3(belief)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_edges_id1 ON %I.edges_v3(id1)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_edges_id2 ON %I.edges_v3(id2)', agent_name, agent_name);

    -- 4. EDGE OBSERVATIONS
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.edge_observations (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            edge_id1 TEXT NOT NULL,
            edge_id2 TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            source_type VARCHAR(50),
            session_id TEXT,
            agent VARCHAR(50),
            platform TEXT,
            weight FLOAT DEFAULT 1.0,
            trust_tier VARCHAR(20) DEFAULT ''self''
        )', agent_name);

    -- 5. TEXT EMBEDDINGS (pgvector)
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.text_embeddings (
            memory_id TEXT PRIMARY KEY,
            embedding halfvec(1024),
            preview TEXT,
            model VARCHAR(100) DEFAULT ''Qwen3-Embedding-0.6B'',
            indexed_at TIMESTAMPTZ DEFAULT NOW()
        )', agent_name);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_text_emb_cosine ON %I.text_embeddings USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)', agent_name, agent_name);

    -- 6. LESSONS
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.lessons (
            id TEXT PRIMARY KEY,
            category VARCHAR(50),
            lesson TEXT NOT NULL,
            evidence TEXT,
            source VARCHAR(50) DEFAULT ''manual'',
            confidence FLOAT DEFAULT 0.7,
            created TIMESTAMPTZ DEFAULT NOW(),
            applied_count INTEGER DEFAULT 0,
            last_applied TIMESTAMPTZ,
            superseded_by TEXT
        )', agent_name);

    -- 7. SESSIONS
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.sessions (
            id SERIAL PRIMARY KEY,
            started TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ended TIMESTAMPTZ,
            is_active BOOLEAN DEFAULT TRUE
        )', agent_name);

    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.session_recalls (
            session_id INTEGER REFERENCES %I.sessions(id),
            memory_id TEXT,
            source VARCHAR(30) DEFAULT ''manual'',
            recalled_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (session_id, memory_id, source)
        )', agent_name, agent_name);

    -- 8. DECAY HISTORY
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.decay_history (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT NOW(),
            decayed INTEGER DEFAULT 0,
            pruned INTEGER DEFAULT 0
        )', agent_name);

    -- 9. TYPED EDGES (semantic relationship graph)
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.typed_edges (
            id SERIAL PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relationship VARCHAR(50) NOT NULL,
            confidence FLOAT DEFAULT 0.8,
            evidence TEXT,
            auto_extracted BOOLEAN DEFAULT FALSE,
            created TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(source_id, target_id, relationship)
        )', agent_name);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_typed_edges_source ON %I.typed_edges(source_id)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_typed_edges_target ON %I.typed_edges(target_id)', agent_name, agent_name);

    -- 10. Q-VALUE HISTORY (Q-learning trajectory tracking)
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.q_value_history (
            id SERIAL PRIMARY KEY,
            memory_id TEXT NOT NULL,
            session_id INTEGER DEFAULT 0,
            old_q FLOAT,
            new_q FLOAT,
            reward FLOAT,
            reward_source TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )', agent_name);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_qvh_memory ON %I.q_value_history(memory_id)', agent_name, agent_name);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%s_qvh_time ON %I.q_value_history(updated_at)', agent_name, agent_name);

    -- 11. KEY-VALUE STORE (catch-all for misc state)
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.key_value_store (
            key VARCHAR(200) PRIMARY KEY,
            value JSONB,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )', agent_name);

    -- 11. CONTEXT GRAPHS (5W projections)
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.context_graphs (
            dimension VARCHAR(20) NOT NULL,
            sub_view VARCHAR(50) NOT NULL DEFAULT '''',
            last_rebuilt TIMESTAMPTZ,
            edge_count INTEGER,
            node_count INTEGER,
            hubs TEXT[],
            stats JSONB,
            edges JSONB,
            PRIMARY KEY (dimension, sub_view)
        )', agent_name);

    RAISE NOTICE 'Schema % created with all tables', agent_name;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- CREATE SHARED SCHEMA (cross-agent data)
-- ============================================================
CREATE SCHEMA IF NOT EXISTS shared;

-- Shared memories — cross-agent knowledge
CREATE TABLE IF NOT EXISTS shared.memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by VARCHAR(50) NOT NULL,
    source_memory_id TEXT,
    tags TEXT[],
    entities JSONB DEFAULT '{}'::jsonb,
    emotional_weight FLOAT DEFAULT 0.5,
    importance DOUBLE PRECISION DEFAULT 0.5
);

CREATE INDEX IF NOT EXISTS idx_shared_memories_created_by ON shared.memories(created_by);
CREATE INDEX IF NOT EXISTS idx_shared_memories_created ON shared.memories(created DESC);
CREATE INDEX IF NOT EXISTS idx_shared_memories_tags ON shared.memories USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_shared_memories_fts ON shared.memories USING GIN(to_tsvector('english', content));

-- Shared text embeddings
CREATE TABLE IF NOT EXISTS shared.text_embeddings (
    memory_id TEXT PRIMARY KEY,
    embedding halfvec(1024),
    preview TEXT,
    model VARCHAR(100) DEFAULT 'Qwen3-Embedding-0.6B',
    indexed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shared_text_emb_cosine ON shared.text_embeddings USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Agent registry
CREATE TABLE IF NOT EXISTS shared.agent_registry (
    name VARCHAR(50) PRIMARY KEY,
    schema_name VARCHAR(50) NOT NULL,
    registered TIMESTAMPTZ DEFAULT NOW(),
    last_active TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Vocabulary bridges (shared across agents)
CREATE TABLE IF NOT EXISTS shared.vocabulary_bridges (
    id SERIAL PRIMARY KEY,
    term1 TEXT NOT NULL,
    term2 TEXT NOT NULL,
    group_name TEXT,
    source VARCHAR(50) DEFAULT 'manual',
    confirmed BOOLEAN DEFAULT FALSE,
    created TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(term1, term2)
);

-- ============================================================
-- CREATE AGENT SCHEMAS
-- ============================================================
SELECT create_agent_schema('max');
SELECT create_agent_schema('beth');
SELECT create_agent_schema('susan');

-- Register agents
INSERT INTO shared.agent_registry (name, schema_name, registered)
VALUES
    ('max_anvil', 'max', NOW()),
    ('bethany_finkel', 'beth', NOW()),
    ('susan_casiodega', 'susan', NOW())
ON CONFLICT (name) DO NOTHING;
