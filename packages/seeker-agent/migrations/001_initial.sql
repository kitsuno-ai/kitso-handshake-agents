-- Migration 001: initial experiment_db schema (design §9.1)
-- Idempotent. Safe to re-run.

BEGIN;

-- Append-only. One row per (venue, post_id) seen by the classifier.
CREATE TABLE IF NOT EXISTS classifications (
    id BIGSERIAL PRIMARY KEY,
    venue TEXT NOT NULL,                -- moltbook | gonzo_* | ...
    submolt_or_channel TEXT,
    post_id TEXT NOT NULL,              -- venue-specific stable id
    observed_at TIMESTAMPTZ NOT NULL,
    classified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_job_shaped BOOLEAN NOT NULL,
    relevance REAL NOT NULL,
    extracted_role_title TEXT,
    extracted_role_family TEXT,
    extracted_seniority TEXT,
    extracted_company TEXT,
    extracted_country_hint TEXT,
    extracted_remote_hint TEXT,
    has_vacancy_card_url BOOLEAN NOT NULL DEFAULT false,
    vacancy_card_url TEXT,
    spam_signals JSONB DEFAULT '[]'::jsonb,
    language_detected TEXT,
    reasoning TEXT,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    latency_ms INTEGER,
    cost_usd_estimate REAL,
    raw_post_excerpt TEXT,
    UNIQUE (venue, post_id)
);

CREATE INDEX IF NOT EXISTS idx_class_venue_classified
    ON classifications (venue, classified_at DESC);
CREATE INDEX IF NOT EXISTS idx_class_job_shaped
    ON classifications (is_job_shaped, relevance DESC)
    WHERE is_job_shaped;
CREATE INDEX IF NOT EXISTS idx_class_prompt_version
    ON classifications (prompt_version, classified_at DESC);

-- Append-only. Outbound actions and gate-drops.
CREATE TABLE IF NOT EXISTS actions (
    id BIGSERIAL PRIMARY KEY,
    classification_id BIGINT REFERENCES classifications(id),
    verb TEXT NOT NULL,                 -- read_vacancy_card | initiate_handshake | post_field_note
    outcome TEXT NOT NULL,              -- ok | dropped_at_gate | http_error | venue_error | measured_only | no_card_url | would_handshake
    gate_name TEXT,                     -- when outcome=dropped_at_gate
    details_jsonb JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_actions_classification ON actions (classification_id);
CREATE INDEX IF NOT EXISTS idx_actions_outcome ON actions (verb, outcome, started_at DESC);

-- Append-only. Anything that failed.
CREATE TABLE IF NOT EXISTS errors (
    id BIGSERIAL PRIMARY KEY,
    arm TEXT NOT NULL,                  -- moltbook | gonzo
    channel TEXT,
    verb TEXT,
    classification_id BIGINT,
    error_class TEXT NOT NULL,          -- json_parse | schema_invalid | http_5xx | timeout | mistral_error | fetch_failed
    error_message TEXT,
    raw_response TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_errors_arm_when ON errors (arm, occurred_at DESC);

-- Dedup table for handshake initiation. One row per (card_url).
CREATE TABLE IF NOT EXISTS cards_seen (
    card_url TEXT PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    handshake_initiated_at TIMESTAMPTZ,
    handshake_outcome TEXT
);

-- Per-arm/channel watermarks. UPDATE only after successful tick.
-- For gonzo arm: last_id_seen holds the ISO timestamp of the latest
-- gonzo_first_seen row processed (matches the verb's watermark column).
CREATE TABLE IF NOT EXISTS watermarks (
    arm TEXT NOT NULL,
    channel TEXT NOT NULL,
    last_id_seen TEXT,
    last_tick_at TIMESTAMPTZ,
    PRIMARY KEY (arm, channel)
);

-- Migration tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description TEXT
);

INSERT INTO schema_migrations (version, description)
VALUES ('001', 'Initial schema per design §9.1: classifications, actions, errors, cards_seen, watermarks')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- Verification
\echo
\echo === Tables ===
\dt

\echo
\echo === Applied migrations ===
SELECT version, applied_at, description FROM schema_migrations ORDER BY version;
