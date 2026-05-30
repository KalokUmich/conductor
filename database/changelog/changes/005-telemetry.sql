--liquibase formatted sql

--changeset conductor:005-iteration-token-usage
--comment: Per-iteration, per-model token usage incl. cache tokens (cache tokens previously reached only Langfuse, never persisted). Populated by the observability swap (Step 04).
CREATE TABLE IF NOT EXISTS iteration_token_usage (
    id                    SERIAL      PRIMARY KEY,
    session_id            VARCHAR     NOT NULL,
    iteration             INTEGER     NOT NULL,
    model                 VARCHAR     NOT NULL,
    input_tokens          INTEGER     NOT NULL DEFAULT 0,
    output_tokens         INTEGER     NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER     NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER     NOT NULL DEFAULT 0,
    created_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_iter_usage_session ON iteration_token_usage(session_id);
--rollback DROP TABLE IF EXISTS iteration_token_usage;

--changeset conductor:005-agent-transcript
--comment: Structured COT/thinking + transcript turns. Replaces the reconstructed, truncated COT in session_traces.trace_json with real SDK ThinkingBlock content. Populated by the observability swap (Step 04).
CREATE TABLE IF NOT EXISTS agent_transcript (
    id                 SERIAL      PRIMARY KEY,
    session_id         VARCHAR     NOT NULL,
    iteration          INTEGER     NOT NULL,
    turn_index         INTEGER     NOT NULL,
    role               VARCHAR     NOT NULL,
    block_type         VARCHAR,
    content            TEXT,
    thinking_signature TEXT,
    tool_name          VARCHAR,
    created_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_transcript_session ON agent_transcript(session_id);
--rollback DROP TABLE IF EXISTS agent_transcript;
