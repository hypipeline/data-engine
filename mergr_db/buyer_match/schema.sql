-- Buyer Match — Postgres schema (Data Engine). See BUYER_MATCH_SPEC.md.
-- Idempotent: safe to re-run.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS buyer_match;

-- Buyers: replica slice + blended embedding (stored).
CREATE TABLE IF NOT EXISTS buyer_match.buyers (
    id                   bigint PRIMARY KEY,       -- source buyers.id
    name                 text,
    description          text,
    investment_thesis    text,
    sector_keywords      text,
    website              text,
    tags                 text,                     -- comma-joined tag names (display + keyword features)
    email_count          int  DEFAULT 0,
    linkedin_count       int  DEFAULT 0,
    no_of_employees      int,
    embedding            vector(1536),
    embed_model          text,
    embed_version        int  DEFAULT 1,           -- bump to force controlled re-embed
    embedding_text_hash  text,                     -- sha256("v{ver}:" + build_text)
    embedded_at          timestamptz,
    synced_at            timestamptz DEFAULT now()
);
-- No HNSW index: matching requires EXACT cosine (parity with the tool's full NumPy scan),
-- and an exact seq-scan over ~16k × 1536-d is only a few ms. (Re-add HNSW if scale grows.)

-- Mandates: metadata only (embedded on-demand, NOT stored).
CREATE TABLE IF NOT EXISTS buyer_match.mandates (
    id                   bigint,
    source_table         text,                     -- 'opportunities' | 'bs_opportunities'
    code                 text,
    project_name         text,
    company_name         text,
    summary              text,
    points               jsonb,
    points_paragraph_top text,
    documents            jsonb,                    -- [{title, document(path)}]
    status               int,
    synced_at            timestamptz DEFAULT now(),
    PRIMARY KEY (source_table, id)
);
CREATE INDEX IF NOT EXISTS mandates_code ON buyer_match.mandates (code);

-- Keywords (+ embedding) for the similar-keywords feature.
-- No HNSW index: the set is large (~85k) but exact cosine scan is only tens of ms and
-- avoids very slow per-row index maintenance on bulk load. (Buyers keep HNSW; smaller set.)
CREATE TABLE IF NOT EXISTS buyer_match.keywords (
    keyword      text PRIMARY KEY,
    embedding    vector(1536),
    embed_model  text,
    buyer_count  int DEFAULT 0,
    embedded_at  timestamptz
);

-- Sync bookkeeping (single row).
CREATE TABLE IF NOT EXISTS buyer_match.sync_state (
    id                     int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_sync_at           timestamptz,
    last_buyers_upserted   int,
    last_buyers_embedded   int,
    last_keywords_embedded int,
    last_mandates_synced   int,
    last_cost_usd          numeric
);
