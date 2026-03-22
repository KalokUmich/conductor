--liquibase formatted sql

--changeset conductor:001-create-repo-tokens
--comment: PAT cache for git workspace authentication
CREATE TABLE IF NOT EXISTS repo_tokens (
    repo_url    VARCHAR     NOT NULL PRIMARY KEY,
    token       TEXT        NOT NULL,
    username    VARCHAR,
    cached_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMP WITH TIME ZONE NOT NULL
);
--rollback DROP TABLE IF EXISTS repo_tokens;

--changeset conductor:001-create-session-traces
--comment: Agent loop session metrics for offline analysis
CREATE TABLE IF NOT EXISTS session_traces (
    id                  SERIAL      PRIMARY KEY,
    session_id          VARCHAR     NOT NULL UNIQUE,
    query               TEXT,
    workspace_path      VARCHAR,
    duration_ms         DOUBLE PRECISION,
    total_input_tokens  INTEGER,
    total_output_tokens INTEGER,
    total_tool_calls    INTEGER,
    iterations_count    INTEGER,
    final_answer_chars  INTEGER,
    error               TEXT,
    trace_json          TEXT,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
--rollback DROP TABLE IF EXISTS session_traces;

--changeset conductor:001-create-audit-logs
--comment: Changeset apply audit trail
CREATE TABLE IF NOT EXISTS audit_logs (
    id              SERIAL      PRIMARY KEY,
    room_id         VARCHAR     NOT NULL,
    summary_id      VARCHAR,
    changeset_hash  VARCHAR     NOT NULL,
    applied_by      VARCHAR     NOT NULL,
    mode            VARCHAR     NOT NULL,
    timestamp       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_audit_logs_room_id ON audit_logs (room_id);
--rollback DROP INDEX IF EXISTS ix_audit_logs_room_id; DROP TABLE IF EXISTS audit_logs;

--changeset conductor:001-create-file-metadata
--comment: Uploaded file metadata
CREATE TABLE IF NOT EXISTS file_metadata (
    id                  VARCHAR     NOT NULL PRIMARY KEY,
    room_id             VARCHAR     NOT NULL,
    user_id             VARCHAR     NOT NULL,
    display_name        VARCHAR     NOT NULL,
    original_filename   VARCHAR     NOT NULL,
    stored_filename     VARCHAR     NOT NULL,
    file_type           VARCHAR     NOT NULL,
    mime_type           VARCHAR     NOT NULL,
    size_bytes          BIGINT      NOT NULL,
    uploaded_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_file_metadata_room_id ON file_metadata (room_id);
--rollback DROP INDEX IF EXISTS ix_file_metadata_room_id; DROP TABLE IF EXISTS file_metadata;

--changeset conductor:001-create-todos
--comment: Room-scoped task/TODO tracking
CREATE TABLE IF NOT EXISTS todos (
    id          VARCHAR     NOT NULL PRIMARY KEY,
    room_id     VARCHAR     NOT NULL,
    title       VARCHAR     NOT NULL,
    description TEXT,
    type        VARCHAR     NOT NULL DEFAULT 'task',
    priority    VARCHAR     NOT NULL DEFAULT 'medium',
    status      VARCHAR     NOT NULL DEFAULT 'open',
    file_path   VARCHAR,
    line_number INTEGER,
    created_by  VARCHAR     NOT NULL DEFAULT '',
    assignee    VARCHAR,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    source      VARCHAR     NOT NULL DEFAULT 'manual',
    source_id   VARCHAR
);
CREATE INDEX IF NOT EXISTS ix_todos_room_id ON todos (room_id);
--rollback DROP INDEX IF EXISTS ix_todos_room_id; DROP TABLE IF EXISTS todos;

--changeset conductor:001-create-integration-tokens
--comment: OAuth tokens for external integrations (Jira, Teams, Slack)
CREATE TABLE IF NOT EXISTS integration_tokens (
    id              SERIAL      PRIMARY KEY,
    user_email      VARCHAR     NOT NULL,
    provider        VARCHAR     NOT NULL,
    access_token    TEXT        NOT NULL,
    refresh_token   TEXT        NOT NULL DEFAULT '',
    expires_at      TIMESTAMP WITH TIME ZONE,
    cloud_id        VARCHAR     NOT NULL DEFAULT '',
    site_url        VARCHAR     NOT NULL DEFAULT '',
    scope           VARCHAR     NOT NULL DEFAULT '',
    metadata_json   TEXT,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_integration_user_provider UNIQUE (user_email, provider)
);
--rollback DROP TABLE IF EXISTS integration_tokens;
