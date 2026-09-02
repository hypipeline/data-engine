-- TDC — the deals half.
--
-- The minimum viable story is a buyer, a seller and a date. Everything else is
-- enrichment, so everything else is nullable. A record that carries only those
-- three is not a degraded story; it is the normal one, and the schema treats it
-- that way rather than as a draft waiting to be completed.

CREATE SCHEMA IF NOT EXISTS tdc;

-- ---------------------------------------------------------------- entity
-- A company, once, however many deals it appears in. The bridge to Mergr is a
-- resolution aid and nothing more: Mergr may say who an entity is, and may never
-- be cited for what a deal did. That rule is enforced in tdc.source.kind, which
-- has no value for it.
CREATE TABLE IF NOT EXISTS tdc.entity (
    id              text PRIMARY KEY,
    name            text NOT NULL,
    legal_name      text,
    kind            text NOT NULL DEFAULT 'company'
                    CHECK (kind IN ('company','sponsor','adviser','lender','family-office')),
    jurisdiction    text,
    website         text,
    domain          text,

    -- No single global company register exists, so identifiers are a list.
    -- Scheme codes follow org-id.guide: GB-COH, NL-KVK, DE-HRB, LEI, SEC-CIK…
    identifiers     jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- The Mergr bridge. Null is a perfectly good answer.
    mergr_firm_id       integer,
    mergr_company_id    integer,
    match_method    text CHECK (match_method IN ('domain','legal_name','name_sector','manual')),
    match_confidence numeric(3,2),
    needs_review    boolean NOT NULL DEFAULT false,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entity_domain_idx ON tdc.entity (domain);
CREATE INDEX IF NOT EXISTS entity_mergr_firm_idx ON tdc.entity (mergr_firm_id);
CREATE INDEX IF NOT EXISTS entity_review_idx ON tdc.entity (needs_review) WHERE needs_review;

-- ---------------------------------------------------------------- deal
CREATE TABLE IF NOT EXISTS tdc.deal (
    id              text PRIMARY KEY,
    slug            text UNIQUE,
    status          text NOT NULL DEFAULT 'source'
                    CHECK (status IN ('source','extracted','verified','drafted',
                                      'edited','review','published','spiked')),

    -- ---- the minimum viable story: these three, and nothing else, are required ----
    --
    -- The name is what a source called the party. The entity id is filled in only
    -- when resolution is certain, so a story can publish with unresolved parties —
    -- it simply does not link to an entity page yet. Resolution must fail to null
    -- rather than to the closest name.
    acquirer_name       text NOT NULL,
    acquirer_entity_id  text REFERENCES tdc.entity(id),
    target_name         text NOT NULL,
    target_entity_id    text REFERENCES tdc.entity(id),

    -- A date is always inferable, because you always know when you found out. What
    -- varies is which event it dates and how precisely, so both are recorded rather
    -- than implied. 'reported' is the floor and is always available.
    date_value      date NOT NULL,
    date_precision  text NOT NULL DEFAULT 'month'
                    CHECK (date_precision IN ('day','month','quarter','year')),
    date_kind       text NOT NULL DEFAULT 'reported'
                    CHECK (date_kind IN ('completed','announced','reported')),

    -- ---- everything below is enrichment and may stay null forever ----
    vendor_name         text,
    vendor_entity_id    text REFERENCES tdc.entity(id),
    deal_type       text,
    sector          text,
    region          text,
    country         text,

    -- Absent is not zero and not "undisclosed but understood to be". Null means no
    -- claim exists, and that publishes as Unknown.
    consideration_amount    numeric,
    consideration_currency  text,
    consideration_display   text,

    headline        text,
    deck            text,
    body            text[],

    published_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- A story published at the minimum grows in place as more is learned; the
    -- append-only note of what changed is how a reader can tell it did.
    updates         jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS deal_status_idx ON tdc.deal (status);
CREATE INDEX IF NOT EXISTS deal_date_idx ON tdc.deal (date_value DESC);
CREATE INDEX IF NOT EXISTS deal_acquirer_idx ON tdc.deal (acquirer_entity_id);
CREATE INDEX IF NOT EXISTS deal_target_idx ON tdc.deal (target_entity_id);

-- ---------------------------------------------------------------- parties
-- Advisers and lenders, beyond the three that make the story. Roles are open text
-- because adviser roles proliferate; side is constrained because it does not.
CREATE TABLE IF NOT EXISTS tdc.deal_party (
    id          bigserial PRIMARY KEY,
    deal_id     text NOT NULL REFERENCES tdc.deal(id) ON DELETE CASCADE,
    entity_id   text REFERENCES tdc.entity(id),
    name        text NOT NULL,
    role        text NOT NULL,
    side        text CHECK (side IN ('buy','sell','target','lender')),
    UNIQUE (deal_id, name, role)
);

-- ---------------------------------------------------------------- source
-- Where a claim came from. Stored text outlives the URL, which will rot.
--
-- `kind` deliberately has no value for Mergr or any other aggregator. An
-- aggregator can raise a lead; it cannot become a citation, and making that
-- impossible in the type is stronger than writing it in a comment.
CREATE TABLE IF NOT EXISTS tdc.source (
    id          text PRIMARY KEY,
    deal_id     text REFERENCES tdc.deal(id) ON DELETE CASCADE,
    kind        text NOT NULL
                CHECK (kind IN ('registry','party','adviser','press','own')),
    url         text,
    publisher   text,
    title       text,

    -- Reliability is a property of the filing type, not of the register as a whole:
    -- a registered charge carries a real consequence for being wrong (s.859H), a
    -- self-reported PSC date does not. Recorded so precedence can tell them apart.
    doc_type    text,
    published_at    timestamptz,
    retrieved_at    timestamptz NOT NULL DEFAULT now(),
    stored_text     text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS source_deal_idx ON tdc.source (deal_id);

-- ---------------------------------------------------------------- claim
-- One source, one field, one value. Immutable: it records what a document said,
-- which stays true even when the document was wrong. Everything downstream is
-- recomputable from claims, so a better extractor means a backfill, not a rewrite.
CREATE TABLE IF NOT EXISTS tdc.claim (
    id          bigserial PRIMARY KEY,
    deal_id     text NOT NULL REFERENCES tdc.deal(id) ON DELETE CASCADE,
    source_id   text NOT NULL REFERENCES tdc.source(id) ON DELETE CASCADE,
    field       text NOT NULL,
    value       jsonb NOT NULL,
    span        text,
    extracted_by    text,
    verified_by     text,
    confidence      numeric(3,2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claim_deal_field_idx ON tdc.claim (deal_id, field);

-- Provenance published against a field. Names where a value came from rather than
-- how confident we are: a reader can weigh "the parties announced it" and cannot
-- weigh "confirmed".
--   documented  a filing exists, and the record says which
--   announced   a party said it — first-hand, and self-interested
--   reported    a third party said it
--   derived     computed here, and it carries its workings
--   unknown     no claim exists
CREATE TABLE IF NOT EXISTS tdc.field_provenance (
    deal_id     text NOT NULL REFERENCES tdc.deal(id) ON DELETE CASCADE,
    field       text NOT NULL,
    provenance  text NOT NULL
                CHECK (provenance IN ('documented','announced','reported','derived','unknown')),
    won_by      text REFERENCES tdc.source(id),
    corroboration   integer NOT NULL DEFAULT 0,
    conflicts   jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (deal_id, field)
);

-- ---------------------------------------------------------------- lead
-- A hypothesis that a deal exists. This is where the pipeline actually starts.
--
-- A lead is not an early deal and not a source: it is a signal that something may
-- have happened, often with no document describing it at all. A PSC cessation at a
-- broker, an adviser posting that they "advised the shareholders of", a name
-- appearing twice in a week. None of those are stories; any of them may become one.
--
-- Leads are cheap and disposable. The point of the table is recall — the cost of
-- missing a deal is total, the cost of dismissing a bad lead is a click.
CREATE TABLE IF NOT EXISTS tdc.lead (
    id          text PRIMARY KEY,

    -- Where it came from. Note that 'aggregator' is permitted HERE and nowhere else:
    -- Mergr may raise a lead, and tdc.source.kind has no value that could ever hold
    -- it as a citation. The separation between the two enums is the whole rule.
    channel     text NOT NULL
                CHECK (channel IN ('filing','press','adviser','party','watchlist',
                                   'aggregator','manual')),
    signal      text NOT NULL,     -- what was noticed, in words a person can triage
    url         text,
    doc_type    text,              -- PSC07, MR01, RSS item, LinkedIn post…

    -- The name as noticed, before any resolution. Usually all there is.
    entity_hint text,
    entity_id   text REFERENCES tdc.entity(id),

    noticed_at  timestamptz NOT NULL DEFAULT now(),
    occurred_at date,              -- if the signal implies a date; frequently null

    -- Triage. A lead is promoted only when it reaches the minimum viable story —
    -- a buyer, a seller and a date — which is exactly the threshold tdc.deal enforces.
    status      text NOT NULL DEFAULT 'new'
                CHECK (status IN ('new','working','promoted','dismissed','duplicate')),
    dismissed_reason text,
    deal_id     text REFERENCES tdc.deal(id),

    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_status_idx ON tdc.lead (status, noticed_at DESC);
CREATE INDEX IF NOT EXISTS lead_channel_idx ON tdc.lead (channel);
CREATE INDEX IF NOT EXISTS lead_entity_idx ON tdc.lead (entity_id);

-- The deal's own pipeline starts once a lead is promoted, so 'source' is no longer
-- the first thing that happens to a story — it is the first thing that happens to a
-- story that already exists.

-- ---------------------------------------------------------------- coverage
-- The firms we watch for leads. Deliberately thin: a name, a website, a LinkedIn
-- page. It is a reading list, not a CRM — nothing about a relationship with the
-- firm belongs here, and none is copied from wherever the roster came from.
--
-- needs_check exists because the LinkedIn Finder never answers "I don't know": it
-- resolved 20 of 20 firms including ones it plainly got wrong, matching Mainstreet
-- Capital to a similarly-named US listed BDC. A low name match is not an error to
-- correct automatically; it is a row for a person to look at.
CREATE TABLE IF NOT EXISTS tdc.coverage (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    website     text,
    linkedin_url text,
    employees   integer,
    origin      text,              -- where the name came from, e.g. on.advisory_firms
    resolved_by text,              -- finder | footer | manual
    name_match  numeric(3,2),      -- firm name vs resolved LinkedIn slug
    needs_check boolean NOT NULL DEFAULT false,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS coverage_active_idx ON tdc.coverage (active, name);
