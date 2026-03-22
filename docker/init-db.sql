-- Create the langfuse database on the shared Postgres instance.
-- The default database (conductor) and user are created by POSTGRES_DB/POSTGRES_USER env vars.
-- The conductor user owns both databases.
-- This script runs once on first container start via /docker-entrypoint-initdb.d/.
--
-- NOTE: This only creates the DATABASES. Table schemas are managed by:
--   - conductor DB:  Liquibase (database/changelog/) — run `make db-update`
--   - langfuse DB:   Langfuse internal Prisma migrations (automatic on startup)

CREATE DATABASE langfuse OWNER conductor;
