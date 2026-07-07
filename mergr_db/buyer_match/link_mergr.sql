-- Rebuild buyer_match.buyer_mergr — the precomputed buyer -> Mergr link.
-- One-time / periodic (re-run after a sync). Set-based, so the whole 16k runs in one pass.
-- Match precedence: firm-by-domain -> firm-by-name -> company-by-domain -> company-by-name.
-- Firms carry precomputed size/AUM/total_buys/largest_buy; companies derive acquisitions +
-- largest from transaction_parties (role='acquirer') joined to transactions.

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
firm_m AS (
    SELECT buyer_id, firm_id, size_category, pe_assets, total_buys, largest_buy, 'domain' AS mb FROM fd
    UNION ALL
    SELECT buyer_id, firm_id, size_category, pe_assets, total_buys, largest_buy, 'name'   AS mb FROM fn
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
SELECT buyer_id, 'firm', firm_id, NULL,
       size_category, pe_assets, total_buys, largest_buy, mb
FROM firm_m
UNION ALL
SELECT cm.buyer_id, 'company', NULL, cm.company_id,
       NULL, NULL,
       (SELECT count(*) FROM transaction_parties tp
          WHERE tp.entity_type='company' AND tp.entity_mergr_id=cm.company_id AND tp.role='acquirer'),
       (SELECT t.target_name ||
               CASE WHEN t.deal_value IS NOT NULL THEN ' ' ||
                    CASE WHEN t.deal_value >= 1e9 THEN '$' || round(t.deal_value/1e9, 1) || 'B'
                         WHEN t.deal_value >= 1e6 THEN '$' || round(t.deal_value/1e6, 0) || 'M'
                         ELSE '$' || round(t.deal_value, 0) END
                    ELSE '' END
          FROM transaction_parties tp JOIN transactions t USING (transaction_id)
          WHERE tp.entity_type='company' AND tp.entity_mergr_id=cm.company_id AND tp.role='acquirer'
          ORDER BY t.deal_value DESC NULLS LAST LIMIT 1),
       cm.mb
FROM comp_m cm;
