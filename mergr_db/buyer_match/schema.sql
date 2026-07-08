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
    email_domains        text[],                   -- distinct CORPORATE contact-email domains (ON side); firm email-domain match key
    is_specialist        boolean,                  -- ON buyers.is_specialist
    embedding            vector(1536),
    embed_model          text,
    embed_version        int  DEFAULT 1,           -- bump to force controlled re-embed
    embedding_text_hash  text,                     -- sha256("v{ver}:" + build_text)
    embedded_at          timestamptz,
    synced_at            timestamptz DEFAULT now()
);
-- No HNSW index: matching requires EXACT cosine (parity with the tool's full NumPy scan),
-- and an exact seq-scan over ~16k × 1536-d is only a few ms. (Re-add HNSW if scale grows.)
ALTER TABLE buyer_match.buyers ADD COLUMN IF NOT EXISTS email_domains text[];
ALTER TABLE buyer_match.buyers ADD COLUMN IF NOT EXISTS is_specialist boolean;

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

-- Query-embedding cache: skip the OpenAI embed for a repeated search text.
-- Embeddings are deterministic per (model, text), so entries never expire.
CREATE TABLE IF NOT EXISTS buyer_match.query_cache (
    query_hash   text PRIMARY KEY,       -- sha256(model + '\n' + trim(text))
    query_text   text,
    model        text,
    embedding    vector(1536),
    hits         int DEFAULT 0,
    created_at   timestamptz DEFAULT now(),
    last_used_at timestamptz DEFAULT now()
);

-- Document-summary cache: skip the gpt-4o-mini summarise for a document we've seen.
-- Keyed on a hash of the (model + storage path); upload paths are immutable, so a hit
-- means the same file. Cached docs cost $0 on re-load of the same mandate.
CREATE TABLE IF NOT EXISTS buyer_match.doc_cache (
    doc_hash          text PRIMARY KEY,   -- sha256(model + '\n' + document path)
    doc_path          text,
    title             text,
    summary           text,
    prompt_tokens     int DEFAULT 0,
    completion_tokens int DEFAULT 0,
    created_at        timestamptz DEFAULT now(),
    hits              int DEFAULT 0
);

-- LinkedIn enrichment side-table (populated by buyer_enrich.py). Kept DELIBERATELY SEPARATE
-- from buyer_match.buyers: sync re-pulls buyers from read-only prod, so anything written onto
-- buyers would be lost — this table (keyed by buyer_id) is never touched by sync, so the
-- enriched data persists. The Buyer Match queries fall back to it via effective_employees().
CREATE TABLE IF NOT EXISTS buyer_match.buyer_linkedin (
    buyer_id     bigint PRIMARY KEY,
    query        text,
    linkedin_url text,
    employees    int,
    name         text,
    website      text,
    address      text,
    status       text,                      -- ok | no_data | no_linkedin | no_input | error
    checked_at   timestamptz DEFAULT now()
);

-- Effective employee count for a buyer: its own value if present, else the cached LinkedIn
-- count. Source always wins (a real synced count beats the fallback). One function, used by
-- every buyer query, so the fallback is transparent to callers and the UI.
CREATE OR REPLACE FUNCTION buyer_match.effective_employees(p_buyer_id bigint, p_src int)
RETURNS int LANGUAGE sql STABLE AS $$
    SELECT COALESCE(p_src,
        (SELECT employees FROM buyer_match.buyer_linkedin WHERE buyer_id = p_buyer_id));
$$;

-- Precomputed buyer -> Mergr link (populated by link_mergr.sql — a one-time / periodic pass).
-- Resolving buyer->Mergr live per query was far too slow (regexp + firm/company lookups over
-- every buyer), so we materialise the match once here. Kept SEPARATE from buyer_match.buyers
-- (keyed by buyer_id) so buyer sync never destroys it. A buyer matches a Mergr FIRM (PE firm —
-- carries size_category + AUM + total_buys/largest_buy) or, failing that, a Mergr COMPANY
-- (operating/strategic buyer — acquisitions + largest derived from transaction_parties).
CREATE TABLE IF NOT EXISTS buyer_match.buyer_mergr (
    buyer_id      bigint PRIMARY KEY,
    kind          text,               -- 'firm' | 'company'
    firm_id       bigint,
    company_id    bigint,
    size_category text,               -- firm only (Small/Middle-Market/Large/Mega)
    aum           text,               -- firm only (pe_assets, e.g. 8.2BUSD)
    acquisitions  int,                -- firm total_buys, or company acquirer count
    largest       text,               -- firm largest_buy, or company's largest acquisition
    matched_by    text,               -- 'domain' | 'name'
    matched_at    timestamptz DEFAULT now()
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
