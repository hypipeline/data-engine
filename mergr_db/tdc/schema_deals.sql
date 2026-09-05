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

-- Bridge validation. Name similarity was the wrong instrument: it cleared
-- Mainstreet Capital -> a similarly-named US listed BDC at 0.73, and flagged
-- AAB -> aab-accountants at 0.33 despite that being correct. Resemblance is not
-- evidence. A reciprocal link is, so both directions are checked and recorded
-- separately rather than collapsed into a score.
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS site_links_linkedin boolean;
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS linkedin_lists_site boolean;
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS bridge text
    CHECK (bridge IN ('both','site_only','linkedin_only','neither','unreachable'));
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS bridge_note text;
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS checked_at timestamptz;

-- Where a firm publishes its own transactions. Corporate finance houses almost all
-- have one, under a dozen different names — transactions, deals, track record,
-- tombstones, credentials — so it is found by reading the site's own navigation
-- rather than guessing paths, and then confirmed by what the page contains.
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS deals_url text;
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS deals_how text;      -- nav | guess
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS deals_label text;    -- what the site calls it
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS deals_signals integer;
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS deals_checked_at timestamptz;

-- One transactions URL per firm. If a person sets it, that is the answer — for
-- good, and including when they set it to nothing. deals_locked simply means a
-- human has spoken, and the scanner skips those rows entirely.
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS deals_locked boolean NOT NULL DEFAULT false;
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS deals_set_by text;
ALTER TABLE tdc.coverage ADD COLUMN IF NOT EXISTS deals_set_at timestamptz;

-- Fold the earlier manual/none columns down into the single field, once.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='tdc' AND table_name='coverage'
               AND column_name='deals_url_manual') THEN
    UPDATE tdc.coverage
       SET deals_url = deals_url_manual, deals_locked = true,
           deals_set_by = deals_manual_by, deals_set_at = deals_manual_at
     WHERE deals_url_manual IS NOT NULL;
    UPDATE tdc.coverage
       SET deals_url = NULL, deals_locked = true,
           deals_set_by = deals_manual_by, deals_set_at = deals_manual_at
     WHERE deals_none;
  END IF;
END $$;

ALTER TABLE tdc.coverage DROP COLUMN IF EXISTS deals_url_manual;
ALTER TABLE tdc.coverage DROP COLUMN IF EXISTS deals_none;
ALTER TABLE tdc.coverage DROP COLUMN IF EXISTS deals_manual_by;
ALTER TABLE tdc.coverage DROP COLUMN IF EXISTS deals_manual_at;
ALTER TABLE tdc.coverage DROP COLUMN IF EXISTS deals_manual_signals;

-- ---------------------------------------------------------------- scan_item
-- One row per thing found on a watched source: a LinkedIn post, or an entry on a
-- firm's transactions page. Raw harvest, before any judgement about whether it is
-- a deal — that is the lead's job, and this is what the lead is made from.
--
-- expanded records whether the text came from a page of its own or from the index
-- it was listed on. Some transactions pages link a write-up per deal; others are a
-- flat list with no click targets, and then a title is all there is. Both are
-- valid, and which one you got changes how much the text can be trusted to carry.
CREATE TABLE IF NOT EXISTS tdc.scan_item (
    id          bigserial PRIMARY KEY,
    coverage_id bigint NOT NULL REFERENCES tdc.coverage(id) ON DELETE CASCADE,
    channel     text NOT NULL CHECK (channel IN ('linkedin','transactions')),
    external_id text NOT NULL,          -- activity urn, or the page slug
    url         text,
    title       text,
    body        text,
    published_at timestamptz,
    expanded    boolean NOT NULL DEFAULT false,
    outlink     text,                   -- shared destination — a dedup key across posts
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (coverage_id, channel, external_id)
);
CREATE INDEX IF NOT EXISTS scan_item_cov_idx ON tdc.scan_item (coverage_id, published_at DESC);

-- ---------------------------------------------------------------- fetch_cache
-- Raw pages, kept so a rescan costs nothing for anything already seen.
--
-- Raw HTML rather than extracted text, deliberately: the same argument the source
-- model makes for stored_text. When the extractor improves, a better result should
-- be a re-parse of what we already hold, not another twenty thousand fetches.
-- Postgres compresses this column out of line, and HTML compresses well.
CREATE TABLE IF NOT EXISTS tdc.fetch_cache (
    url         text PRIMARY KEY,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    status      integer,
    bytes       integer,
    body        text,
    hits        integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS fetch_cache_age_idx ON tdc.fetch_cache (fetched_at);

-- What a post or deal page carries besides its text. Links are the strongest
-- entity signal available for free — a deal page linking claranet.com resolves the
-- buyer without matching a single name — and images are the tombstone logos.
ALTER TABLE tdc.scan_item ADD COLUMN IF NOT EXISTS links jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE tdc.scan_item ADD COLUMN IF NOT EXISTS images jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE tdc.scan_item ADD COLUMN IF NOT EXISTS body_from text;   -- segment | og

-- The whole readable page, not the three-line summary the list view shows. The
-- raw HTML already sits in fetch_cache, but this is the extracted form the
-- classifier and the claim extractor will actually read, so it is worth holding
-- next to the item and worth being able to eyeball.
ALTER TABLE tdc.scan_item ADD COLUMN IF NOT EXISTS full_text text;
ALTER TABLE tdc.scan_item ADD COLUMN IF NOT EXISTS full_chars integer;

-- How much of a page was template rather than content. Kept so the extraction can
-- be judged: a page that is 90% chrome is either badly extracted or barely a page.
ALTER TABLE tdc.scan_item ADD COLUMN IF NOT EXISTS chrome_chars integer;

-- ---------------------------------------------------------------- scan_run
-- One row per source per scan, so a source that quietly stops working is visible.
--
-- "Nothing new" is ambiguous on its own: it is what a quiet fortnight looks like
-- and also what a broken selector looks like. What separates them is whether the
-- items we already had came back. A source returning the same twelve items is
-- working and quiet; one returning none, or twelve different ones, has changed.
CREATE TABLE IF NOT EXISTS tdc.scan_run (
    id            bigserial PRIMARY KEY,
    coverage_id   bigint NOT NULL REFERENCES tdc.coverage(id) ON DELETE CASCADE,
    channel       text NOT NULL,
    ran_at        timestamptz NOT NULL DEFAULT now(),
    ok            boolean NOT NULL DEFAULT true,
    error         text,
    items_found   integer NOT NULL DEFAULT 0,
    items_new     integer NOT NULL DEFAULT 0,
    items_seen    integer NOT NULL DEFAULT 0,   -- returned again, already held
    items_missing integer NOT NULL DEFAULT 0,   -- held before, absent this time
    content_chars integer NOT NULL DEFAULT 0,
    chrome_chars  integer NOT NULL DEFAULT 0,
    health        text,                          -- ok | quiet | degraded | broken
    health_note   text
);
CREATE INDEX IF NOT EXISTS scan_run_cov_idx ON tdc.scan_run (coverage_id, channel, ran_at DESC);
