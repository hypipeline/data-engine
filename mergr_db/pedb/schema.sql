-- PE DB — Postgres schema (Data Engine). Backend for The Deal Chronicle.
-- Idempotent: safe to re-run.
CREATE SCHEMA IF NOT EXISTS pedb;

-- Subscribers: a replica slice of the DynamoDB table the public sign-up Lambda
-- writes to. DynamoDB stays the upstream source of truth so that sign-ups keep
-- working when Data Engine is down; Postgres holds the queryable copy (same
-- pattern as buyer_match's replica of origryxd_main).
CREATE TABLE IF NOT EXISTS pedb.subscribers (
    email            text PRIMARY KEY,
    domain           text,                     -- split from email; the join key to firms/companies
    status           text,                     -- pending | confirmed | unsubscribed
    cadence          text,                     -- 'standard' until a publishing rhythm is chosen
    regions          text[],
    sectors          text[],
    created_at       timestamptz,
    confirmed_at     timestamptz,
    unsubscribed_at  timestamptz,
    sg_synced        boolean,                  -- reached SendGrid's contact list?
    sg_error         text,
    synced_at        timestamptz DEFAULT now() -- when this row was last pulled from DynamoDB
);
CREATE INDEX IF NOT EXISTS subscribers_status_idx  ON pedb.subscribers (status);
CREATE INDEX IF NOT EXISTS subscribers_domain_idx  ON pedb.subscribers (domain);
CREATE INDEX IF NOT EXISTS subscribers_created_idx ON pedb.subscribers (created_at DESC);

-- One row per sync run, so the UI can show "last synced" and what changed.
CREATE TABLE IF NOT EXISTS pedb.sync_runs (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL,                 -- 'dynamodb:dealchronicle-subscribers'
    started_at  timestamptz DEFAULT now(),
    finished_at timestamptz,
    scanned     int DEFAULT 0,
    inserted    int DEFAULT 0,
    updated     int DEFAULT 0,
    removed     int DEFAULT 0,
    ok          boolean,
    error       text
);
CREATE INDEX IF NOT EXISTS sync_runs_source_idx ON pedb.sync_runs (source, started_at DESC);
