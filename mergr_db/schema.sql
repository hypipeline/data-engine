-- Mergr relationship database schema
-- Postgres + pgvector. Embedding dim defaults to 1536 (OpenAI text-embedding-3-small).
-- Switch to 3072 for text-embedding-3-large (and re-run / re-embed).

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- firms  (investors)  -- mergr.com/firms/<id>
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS firms (
    firm_id                         BIGINT PRIMARY KEY,          -- = mergr_id
    name                            TEXT,
    legal_name                      TEXT,
    address_raw                     TEXT,
    website                         TEXT,
    email                           TEXT,
    phone                           TEXT,
    linkedin                        TEXT,
    investor_type                   TEXT,
    ownership                       TEXT,
    size_category                   TEXT,
    pe_assets                       TEXT,
    established                     TEXT,
    specialist_generalist           TEXT,
    investment_criteria_description TEXT,
    sectors_of_interest             TEXT,
    target_transaction_types        TEXT,
    geographic_preferences          TEXT,
    target_revenue_min              NUMERIC,
    target_revenue_max              NUMERIC,
    target_ebitda_min               NUMERIC,
    target_ebitda_max               NUMERIC,
    investment_size_min             NUMERIC,
    investment_size_max             NUMERIC,
    enterprise_value_min            NUMERIC,
    enterprise_value_max            NUMERIC,
    criteria_currency               TEXT,
    buy_rate_per_year               NUMERIC,
    total_buys                      INTEGER,
    sell_rate_per_year              NUMERIC,
    total_sells                     INTEGER,
    total_buy_volume                TEXT,
    largest_buy                     TEXT,
    total_sell_volume               TEXT,
    largest_sell                    TEXT,
    ma_by_sector                    JSONB,
    raw                             JSONB,
    imported_at                     TIMESTAMPTZ DEFAULT now(),
    criteria_embedding              vector(1536)
);

-- ---------------------------------------------------------------------------
-- companies  (operating companies)  -- mergr.com/company/<id>
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    company_id            BIGINT PRIMARY KEY,                    -- = mergr_id
    name                  TEXT,
    legal_name            TEXT,
    street                TEXT,
    city                  TEXT,
    state_full            TEXT,
    postal_code           TEXT,
    phone                 TEXT,
    website               TEXT,
    sector                TEXT,
    established           TEXT,
    description           TEXT,
    investor_count        INTEGER,
    raw                   JSONB,
    imported_at           TIMESTAMPTZ DEFAULT now(),
    description_embedding  vector(1536)
);

-- ---------------------------------------------------------------------------
-- transactions  -- mergr.com/transactions/<id>
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id      BIGINT PRIMARY KEY,
    date                DATE,
    transaction_url     TEXT,
    transaction_type    TEXT,
    target_mergr_id     BIGINT,                                  -- -> companies.company_id (logical)
    target_name         TEXT,
    target_sector       TEXT,
    target_location     TEXT,
    target_description  TEXT,
    raw                 JSONB,
    imported_at         TIMESTAMPTZ DEFAULT now(),
    target_embedding    vector(1536)
);

-- ---------------------------------------------------------------------------
-- transaction_parties  (acquirers + sellers; each is a firm OR a company)
-- Polymorphic link: (entity_type, entity_mergr_id) -> firms.firm_id | companies.company_id
-- No hard FK because the parent table varies; gap_analysis.py reconciles.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transaction_parties (
    id               BIGSERIAL PRIMARY KEY,
    transaction_id   BIGINT NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('acquirer','seller')),
    entity_type      TEXT NOT NULL CHECK (entity_type IN ('firms','company')),
    entity_mergr_id  BIGINT NOT NULL,
    name             TEXT,
    label            TEXT,
    sub_type         TEXT,
    UNIQUE (transaction_id, role, entity_type, entity_mergr_id)
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tx_target          ON transactions (target_mergr_id);
CREATE INDEX IF NOT EXISTS idx_tx_date            ON transactions (date);
CREATE INDEX IF NOT EXISTS idx_tx_type            ON transactions (transaction_type);
CREATE INDEX IF NOT EXISTS idx_party_entity       ON transaction_parties (entity_type, entity_mergr_id);
CREATE INDEX IF NOT EXISTS idx_party_tx           ON transaction_parties (transaction_id);
CREATE INDEX IF NOT EXISTS idx_party_role         ON transaction_parties (role);
CREATE INDEX IF NOT EXISTS idx_firms_name         ON firms (lower(name));
CREATE INDEX IF NOT EXISTS idx_companies_name     ON companies (lower(name));

-- Vector indexes (HNSW, cosine). Cheap to create empty; populated once embeddings land.
CREATE INDEX IF NOT EXISTS idx_firms_criteria_vec
    ON firms USING hnsw (criteria_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_companies_desc_vec
    ON companies USING hnsw (description_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_tx_target_vec
    ON transactions USING hnsw (target_embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Convenience view: every party with whether we actually hold its record
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_party_resolution AS
SELECT
    p.transaction_id,
    p.role,
    p.entity_type,
    p.entity_mergr_id,
    p.name,
    CASE
        WHEN p.entity_type = 'firms'   AND f.firm_id    IS NOT NULL THEN TRUE
        WHEN p.entity_type = 'company' AND c.company_id IS NOT NULL THEN TRUE
        ELSE FALSE
    END AS have_record
FROM transaction_parties p
LEFT JOIN firms     f ON p.entity_type = 'firms'   AND f.firm_id    = p.entity_mergr_id
LEFT JOIN companies c ON p.entity_type = 'company' AND c.company_id = p.entity_mergr_id;
