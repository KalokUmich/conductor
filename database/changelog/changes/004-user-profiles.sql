--liquibase formatted sql

--changeset conductor:004-user-profiles
--comment: Add persistent user profiles for cross-session identity

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    display_name VARCHAR(255),
    auth_provider VARCHAR(50) NOT NULL,
    avatar_color INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

--rollback DROP TABLE IF EXISTS users;
