-- Rebuild buyer_match.buyer_mergr — the precomputed buyer -> Mergr link.
-- One-time / periodic (re-run after a sync). Set-based, so the whole 16k runs in one pass.
-- Match precedence: firm-by-domain -> firm-by-name -> firm-by-shared-corporate-email-domain
--   -> company-by-domain -> company-by-name.
-- Firms carry precomputed size/AUM/total_buys/largest_buy; companies derive acquisitions +
-- largest from transaction_parties (role='acquirer') joined to transactions.
-- The email-domain pass matches a buyer's corporate contact-email domains (buyers.email_domains,
-- sampled at sync) against a firm's domain/website/email domains — free providers excluded
-- (buyer_match.is_free_email_domain), single-firm domains only. Requires email_domains.ensure_schema().

TRUNCATE buyer_match.buyer_mergr;

WITH b AS (
    SELECT id,
           lower(btrim(name)) AS nm,
           split_part(lower(regexp_replace(regexp_replace(coalesce(website,''),
                      '^https?://',''), '^www\.','')), '/', 1) AS dom
    FROM buyer_match.buyers WHERE embedding IS NOT NULL
),
fd AS (   -- firm by domain
    SELECT DISTINCT ON (b.id) b.id AS buyer_id, f.firm_id,
           f.size_category, f.pe_assets, f.total_buys, f.largest_buy
    FROM b JOIN firms f ON b.dom <> '' AND lower(f.domain) = b.dom
    ORDER BY b.id, f.firm_id
),
fn AS (   -- firm by exact name (buyers not matched by domain)
    SELECT DISTINCT ON (b.id) b.id AS buyer_id, f.firm_id,
           f.size_category, f.pe_assets, f.total_buys, f.largest_buy
    FROM b JOIN firms f ON b.nm <> '' AND lower(btrim(f.name)) = b.nm
    WHERE b.id NOT IN (SELECT buyer_id FROM fd)
    ORDER BY b.id, f.firm_id
),
-- firm CORPORATE candidate domains: the domain field, the website domain, and the email domain.
fdoms AS (
    SELECT firm_id, dom FROM (
        SELECT firm_id, lower(domain) AS dom FROM firms WHERE coalesce(domain,'') <> ''
        UNION
        SELECT firm_id, split_part(lower(regexp_replace(regexp_replace(website,'^https?://',''),'^www\.','')),'/',1)
          FROM firms WHERE coalesce(website,'') <> ''
        UNION
        SELECT firm_id, lower(btrim(regexp_replace(email,'^.*@',''))) FROM firms WHERE email LIKE '%@%'
    ) x
    WHERE dom <> '' AND NOT buyer_match.is_free_email_domain(dom)
),
-- only domains that identify EXACTLY ONE firm — never link on a domain shared by several firms
fdoms_u AS (
    SELECT dom, min(firm_id) AS firm_id FROM fdoms GROUP BY dom HAVING count(DISTINCT firm_id) = 1
),
-- buyer CORPORATE candidate domains (website + sampled contact-email domains), for buyers NOT
-- already matched to a firm by domain or name.
bdoms AS (
    SELECT DISTINCT buyer_id, dom FROM (
        SELECT b.id AS buyer_id, b.dom FROM b WHERE b.dom <> ''
        UNION
        SELECT id AS buyer_id, lower(unnest(email_domains)) FROM buyer_match.buyers WHERE email_domains IS NOT NULL
    ) y
    WHERE dom <> '' AND NOT buyer_match.is_free_email_domain(dom)
      AND buyer_id NOT IN (SELECT buyer_id FROM fd)
      AND buyer_id NOT IN (SELECT buyer_id FROM fn)
),
fe AS (   -- firm by a SHARED corporate domain (rescues e.g. HIG: buyer @higcapital.com = firm email domain)
    SELECT DISTINCT ON (bd.buyer_id) bd.buyer_id, f.firm_id,
           f.size_category, f.pe_assets, f.total_buys, f.largest_buy
    FROM bdoms bd JOIN fdoms_u fu ON fu.dom = bd.dom JOIN firms f ON f.firm_id = fu.firm_id
    ORDER BY bd.buyer_id, f.firm_id
),
firm_m AS (
    SELECT buyer_id, firm_id, size_category, pe_assets, total_buys, largest_buy, 'domain'       AS mb FROM fd
    UNION ALL
    SELECT buyer_id, firm_id, size_category, pe_assets, total_buys, largest_buy, 'name'         AS mb FROM fn
    UNION ALL
    SELECT buyer_id, firm_id, size_category, pe_assets, total_buys, largest_buy, 'email_domain' AS mb FROM fe
),
cd AS (   -- company by domain (buyers not matched to any firm)
    SELECT DISTINCT ON (b.id) b.id AS buyer_id, c.company_id
    FROM b JOIN companies c ON b.dom <> '' AND lower(c.domain) = b.dom
    WHERE b.id NOT IN (SELECT buyer_id FROM firm_m)
    ORDER BY b.id, c.company_id
),
cn AS (   -- company by exact name (still unmatched)
    SELECT DISTINCT ON (b.id) b.id AS buyer_id, c.company_id
    FROM b JOIN companies c ON b.nm <> '' AND lower(btrim(c.name)) = b.nm
    WHERE b.id NOT IN (SELECT buyer_id FROM firm_m)
      AND b.id NOT IN (SELECT buyer_id FROM cd)
    ORDER BY b.id, c.company_id
),
comp_m AS (
    SELECT buyer_id, company_id, 'domain' AS mb FROM cd
    UNION ALL
    SELECT buyer_id, company_id, 'name'   AS mb FROM cn
)
INSERT INTO buyer_match.buyer_mergr
    (buyer_id, kind, firm_id, company_id, size_category, aum, acquisitions, largest, matched_by)
-- `largest` = price paid only (no target entity). Firms: extract the $amount from the
-- precomputed largest_buy text. Companies: format the deal_value of the biggest acquirer deal.
SELECT buyer_id, 'firm', firm_id, NULL,
       size_category, pe_assets, total_buys,
       substring(largest_buy from '\$[0-9][0-9.,]*[BMK]?'), mb
FROM firm_m
UNION ALL
SELECT cm.buyer_id, 'company', NULL, cm.company_id,
       NULL, NULL,
       (SELECT count(*) FROM transaction_parties tp
          WHERE tp.entity_type='company' AND tp.entity_mergr_id=cm.company_id AND tp.role='acquirer'),
       -- Largest acquisition, USD-normalized via fx_rates — same mechanism as the Mergr company
       -- page (deal_value * usd_per_unit). Ranks correctly across currencies and shows USD,
       -- e.g. PTSG's £14M -> $18M. deal_value is in millions.
       (SELECT CASE WHEN u.usd >= 1000 THEN '$' || round(u.usd/1000, 1) || 'B'
                    WHEN u.usd >= 1    THEN '$' || round(u.usd, 0) || 'M'
                    ELSE '$' || round(u.usd*1000, 0) || 'K' END
          FROM (SELECT t.deal_value * fx.usd_per_unit AS usd
                FROM transaction_parties tp JOIN transactions t USING (transaction_id)
                     JOIN fx_rates fx ON fx.currency = t.deal_value_currency
                WHERE tp.entity_type='company' AND tp.entity_mergr_id=cm.company_id
                  AND tp.role='acquirer' AND t.deal_value > 0 AND fx.usd_per_unit IS NOT NULL) u
          ORDER BY u.usd DESC LIMIT 1),
       cm.mb
FROM comp_m cm;
