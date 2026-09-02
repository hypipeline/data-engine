-- TDC — Postgres schema (Data Engine). Backend for The Deal Chronicle.
-- Idempotent: safe to re-run.
CREATE SCHEMA IF NOT EXISTS tdc;

-- Subscribers: a replica slice of the DynamoDB table the public sign-up Lambda
-- writes to. DynamoDB stays the upstream source of truth so that sign-ups keep
-- working when Data Engine is down; Postgres holds the queryable copy (same
-- pattern as buyer_match's replica of origryxd_main).
CREATE TABLE IF NOT EXISTS tdc.subscribers (
    email            text PRIMARY KEY,
    domain           text,                     -- split from email; the join key to firms/companies
    status           text,                     -- pending | confirmed | unsubscribed
    cadence          text,                     -- 'standard' until a publishing rhythm is chosen
    regions          text[],
    sectors          text[],
    created_at       timestamptz,
    confirmed_at     timestamptz,
    unsubscribed_at  timestamptz,
    delivery         text,                     -- ok | sandboxed | complained | blocked
    bounce_type      text,                     -- hard | blocked | dropped
    bounce_reason    text,
    sandboxed_at     timestamptz,
    complained_at    timestamptz,
    synced_at        timestamptz DEFAULT now() -- when this row was last pulled from DynamoDB
);
CREATE INDEX IF NOT EXISTS subscribers_status_idx  ON tdc.subscribers (status);
CREATE INDEX IF NOT EXISTS subscribers_domain_idx  ON tdc.subscribers (domain);
CREATE INDEX IF NOT EXISTS subscribers_created_idx ON tdc.subscribers (created_at DESC);

-- One row per sync run, so the UI can show "last synced" and what changed.
CREATE TABLE IF NOT EXISTS tdc.sync_runs (
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
CREATE INDEX IF NOT EXISTS sync_runs_source_idx ON tdc.sync_runs (source, started_at DESC);

-- Consent and deliverability are independent axes: an address can be confirmed and
-- sandboxed at once, and neither may mask the other. Sending requires both clear.
ALTER TABLE tdc.subscribers ADD COLUMN IF NOT EXISTS delivery text;
ALTER TABLE tdc.subscribers ADD COLUMN IF NOT EXISTS bounce_type text;
ALTER TABLE tdc.subscribers ADD COLUMN IF NOT EXISTS bounce_reason text;
ALTER TABLE tdc.subscribers ADD COLUMN IF NOT EXISTS sandboxed_at timestamptz;
ALTER TABLE tdc.subscribers ADD COLUMN IF NOT EXISTS complained_at timestamptz;
-- Who lifted a sandbox, and when. Kept after the lift: if the address bounces
-- again, that it was manually cleared before is the first thing to know.
ALTER TABLE tdc.subscribers ADD COLUMN IF NOT EXISTS unsandboxed_at timestamptz;
ALTER TABLE tdc.subscribers ADD COLUMN IF NOT EXISTS unsandboxed_by text;
ALTER TABLE tdc.subscribers DROP COLUMN IF EXISTS sg_synced;
ALTER TABLE tdc.subscribers DROP COLUMN IF EXISTS sg_error;

-- Indexes on the columns added above must follow the ALTERs, not sit with the
-- CREATE TABLE: on an existing table the CREATE TABLE is a no-op, so an index
-- declared there runs against the old shape and rolls the whole file back.
CREATE INDEX IF NOT EXISTS subscribers_delivery_idx ON tdc.subscribers (delivery);
