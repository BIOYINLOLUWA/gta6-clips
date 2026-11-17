-- Clip pipeline tables. Safe to run on the existing gta6-content-engine database.
-- Run once:  psql "$DATABASE_URL" -f db/schema.sql

CREATE TABLE IF NOT EXISTS clip_sources (
    id            BIGSERIAL PRIMARY KEY,
    url           TEXT UNIQUE NOT NULL,
    channel       TEXT NOT NULL,
    title         TEXT NOT NULL,
    campaign      TEXT,                       -- Whop campaign name, NULL for own content
    layout        TEXT NOT NULL DEFAULT 'blur',
    status        TEXT NOT NULL DEFAULT 'new',  -- new | processed | blocked | failed
    block_reason  TEXT,
    duration_s    INTEGER,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS clips (
    id                 BIGSERIAL PRIMARY KEY,
    source_id          BIGINT NOT NULL REFERENCES clip_sources(id),
    start_s            REAL NOT NULL,
    end_s              REAL NOT NULL,
    score              REAL NOT NULL,
    transcript         TEXT,
    hook               TEXT,
    captions           JSONB NOT NULL DEFAULT '[]',   -- 3 caption variants
    -- Telegram doubles as file storage: file_ids are re-downloadable later
    tg_short_file_id   TEXT,
    tg_long_file_id    TEXT,
    tg_message_id      BIGINT,
    status             TEXT NOT NULL DEFAULT 'rendered', -- rendered | pending_review | approved | rejected | scheduled | failed
    chosen_caption     INTEGER,
    scheduled_for      JSONB,                        -- {"tiktok": iso, "youtube": iso, "instagram": iso}
    post_response      JSONB,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS clips_status_idx ON clips(status);

CREATE TABLE IF NOT EXISTS clip_kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
