--liquibase formatted sql

--changeset conductor:006-task-hierarchy
--comment: Hierarchical task tree for Brain orchestration (Step 06d). One row per dispatched task — the root coordinator plus each sub-agent — linked parent->child + root, with a per-task token-usage rollup. Closes the per-worker cost-recording gap: SDK leaf-worker usage previously came back via ResultMessage.usage -> budget_summary but was never persisted. CoT tree = recursive query on parent_task_id.
CREATE TABLE IF NOT EXISTS task (
    task_id               VARCHAR     PRIMARY KEY,
    parent_task_id        VARCHAR     REFERENCES task(task_id),
    root_task_id          VARCHAR     NOT NULL,
    session_id            VARCHAR,
    kind                  VARCHAR     NOT NULL,                     -- root | coordinator | sub_agent
    agent_name            VARCHAR,
    query                 TEXT,
    depth                 INTEGER     NOT NULL DEFAULT 0,
    engine                VARCHAR,                                  -- in_house | sdk
    model                 VARCHAR,
    status                VARCHAR     NOT NULL DEFAULT 'running',   -- running | done | error | timeout
    input_tokens          INTEGER     NOT NULL DEFAULT 0,
    output_tokens         INTEGER     NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER     NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER     NOT NULL DEFAULT 0,
    tool_calls            INTEGER     NOT NULL DEFAULT 0,
    iterations            INTEGER     NOT NULL DEFAULT 0,
    duration_ms           DOUBLE PRECISION,
    error                 TEXT,
    started_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    ended_at              TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_task_root    ON task(root_task_id);
CREATE INDEX IF NOT EXISTS idx_task_parent  ON task(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_task_session ON task(session_id);
--rollback DROP TABLE IF EXISTS task;
