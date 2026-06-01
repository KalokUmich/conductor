--liquibase formatted sql

--changeset conductor:007-task-cost
-- USD budget economy: persist per-task cost alongside the existing token columns
-- so the monitoring table records BOTH token usage AND dollars. cost_source marks
-- whether the figure is the SDK's authoritative total_cost_usd ("sdk") or computed
-- from tokens via the pricing table ("computed").
ALTER TABLE task ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION DEFAULT 0;
ALTER TABLE task ADD COLUMN IF NOT EXISTS cost_source VARCHAR(20);
--rollback ALTER TABLE task DROP COLUMN IF EXISTS cost_usd;
--rollback ALTER TABLE task DROP COLUMN IF EXISTS cost_source;
