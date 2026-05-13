-- Migration 002: audit_events table (S299)
-- Generic event log for observability — rate-limit headers, tick lifecycle,
-- fetch/classifier failures. Per-post events are NOT persisted here (they
-- duplicate the classifications + actions tables).
-- Idempotent. Safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type TEXT NOT NULL,           -- rate_limit_observation | tick_complete | tick_aborted | fetch_failed | ...
    arm TEXT,                            -- moltbook | gonzo | NULL
    channel TEXT,                        -- specific channel/submolt or NULL
    classification_id BIGINT,            -- optional FK-style link; not enforced (audit events may outlive rows)
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_events_type_when ON audit_events (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_when ON audit_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_channel_when ON audit_events (channel, occurred_at DESC) WHERE channel IS NOT NULL;

GRANT SELECT, INSERT ON audit_events TO seeker_writer;
GRANT USAGE, SELECT ON SEQUENCE audit_events_id_seq TO seeker_writer;

INSERT INTO schema_migrations (version, description)
VALUES ('002', 'S299: audit_events table for observability (rate-limit, tick lifecycle, errors)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

\echo
\echo === Tables ===
\dt

\echo
\echo === audit_events grants ===
SELECT grantee, privilege_type FROM information_schema.table_privileges
WHERE table_name = 'audit_events' AND grantee = 'seeker_writer'
ORDER BY privilege_type;

\echo
\echo === Applied migrations ===
SELECT version, applied_at, description FROM schema_migrations ORDER BY version;
