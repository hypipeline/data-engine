# Entity Lookup — Output Format

## Design Principles

1. Output is reviewed by a human. It should read like a short research memo, not a JSON blob.
2. Lead with the answer, then show your evidence, then flag risks.
3. **Every factual claim must have a clickable link to the source.** The reviewer should be able to verify any statement without re-doing the research. No link = no claim.

---

## Output Structure

```
ENTITY LOOKUP REPORT
====================

INPUT
  URL: https://www.gemny.com/
  Date: 2026-06-04

─────────────────────────────────────────

RECOMMENDATION
  Entity:       GEM Global Yield LLC SCS
  Jurisdiction: Luxembourg (RCS B173296)
  Status:       Active
  Confidence:   MEDIUM
  Verify:       https://www.northdata.com/GEM%20Global%20Yield%20LLC%20SCS,%20Luxembourg/B173296

  Note: Multiple GEM entities exist. This is the active investment vehicle.
        TopCo is GEM Capital Investments Sàrl (France) but capitalised at
        only €1,000 — substance concern. US operating entity (Global Emerging
        Markets North America Inc., DE file 2351172) also exists but current
        status unconfirmed.

─────────────────────────────────────────

EVIDENCE

  1. URL → Candidate Name
     Website describes "Global Emerging Markets (GEM)" and references
     "GEM Global Yield LLC SCS" in transaction announcements.
     → https://www.gemny.com/
     Quality: INFERRED (company-controlled source)

  2. Candidate → Registry (Luxembourg)
     GEM Global Yield LLC SCS confirmed in Luxembourg RCS as B173296.
     → https://www.northdata.com/GEM%20Global%20Yield%20LLC%20SCS,%20Luxembourg/B173296
     Quality: VERIFIED

  3. Candidate → Registry (SEC)
     GEM Global Yield LLC SCS is an SEC filer (CIK 0001940719).
     Files Forms 3/4 as beneficial owner of public company shares.
     → https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001940719
     Quality: VERIFIED

  4. Reverse: Entity → URL
     SEC filings by GEM Global Yield LLC SCS list business address as
     New York, NY — consistent with gemny.com. Contact email on North Data
     is info@gem-grp.com (note: different domain).
     → https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001940719
     Quality: MODERATE

─────────────────────────────────────────

CORPORATE STRUCTURE

  GEM Capital Investments Sàrl (Paris, France)
    → https://www.northdata.com/GEM%20Capital%20Investments%20S%C3%A0rl,%20Paris,%20FR
  └── owns ≥75% → GEM Global Yield LLC SCS (Luxembourg)  ← RECOMMENDED
                   → https://www.northdata.com/GEM%20Global%20Yield%20LLC%20SCS,%20Luxembourg/B173296
                   └── GEM Global Yield Fund LLC (Delaware, US)
                       → https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001940717

  Also in group:
  • Global Emerging Markets, Incorporated (DE file 2279024)
    → https://icis.corp.delaware.gov/ecorp/entitysearch/NameSearch.aspx [search: "Global Emerging Markets"]
  • Global Emerging Markets North America Inc. (DE file 2351172)
    → https://icis.corp.delaware.gov/ecorp/entitysearch/NameSearch.aspx [search: "Global Emerging Markets North America"]
  • GEM Management Ltd. (UK, Companies House 07015161)
    → https://find-and-update.company-information.service.gov.uk/company/07015161
  • GEM Advisors B.V. (Netherlands, KVK 70147221)
    → https://www.northdata.com/GEM%20Advisors%20B%C2%B7V%C2%B7,%20Amsterdam/KVK%2070147221
  • + 15 other Delaware fund vehicles

  Key Person: Christopher F. Brown (MD & Beneficial Owner)

─────────────────────────────────────────

SUBSTANCE ASSESSMENT

  Score: 58/100 (MEDIUM)

  ✓ Entity confirmed in official register (Luxembourg RCS)
    → https://www.northdata.com/GEM%20Global%20Yield%20LLC%20SCS,%20Luxembourg/B173296
  ✓ Active SEC filer (Forms 3/4 — beneficial ownership)
    → https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001940719
  ✓ Entity age: 10+ years (registered 2012)
  ✓ Multiple jurisdictions: LU, FR, US, UK, NL
  ✗ TopCo capitalised at only €1,000
    → https://www.northdata.com/GEM%20Capital%20Investments%20S%C3%A0rl,%20Paris,%20FR
  ✗ Website domain (gemny.com) differs from entity contact (gem-grp.com)
  ✗ Corporate structure complex — 17+ entities across 4 jurisdictions
  ? US operating entity status unconfirmed (no Delaware status check)

─────────────────────────────────────────

OTHER ENTITIES CONSIDERED

  Entity                                     | Why not recommended                | Verify
  -------------------------------------------|------------------------------------|---------
  GEM Capital Investments Sàrl (FR)          | TopCo but €1,000 capital           | https://www.northdata.com/GEM%20Capital%20Investments%20S%C3%A0rl,%20Paris,%20FR
  Global Emerging Markets North America Inc. | Status unconfirmed in Delaware      | https://icis.corp.delaware.gov/ecorp/entitysearch/NameSearch.aspx
  Global Emerging Markets, Incorporated      | Oldest entity, current role unclear | https://icis.corp.delaware.gov/ecorp/entitysearch/NameSearch.aspx
  GEM Management Ltd. (GB)                   | Appears to be UK office only       | https://find-and-update.company-information.service.gov.uk/company/07015161

─────────────────────────────────────────

SOURCES USED
  • WHOIS lookup (gemny.com)
  • Website extraction (gemny.com)
  • SEC EDGAR full-text search
  • North Data Premium (authenticated)
  • Delaware Division of Corporations (via Browserbase)

RAW DATA: [link to full JSON if needed]
```

---

## Key Differences from v1/v2

1. **Recommendation up front** — reviewer sees the answer in 2 seconds, with a direct "Verify" link
2. **Confidence as a word** (HIGH / MEDIUM / LOW / INSUFFICIENT) not a number — faster to parse
3. **Note field** — plain English explaining why this entity was chosen and what the caveats are
4. **Evidence as narrative with links** — numbered steps telling a story, each with a clickable source URL
5. **Corporate structure as a tree with links** — visual hierarchy, every entity has a link to its registry page
6. **Substance assessment with checkmarks and links** — each ✓/✗ factor links to the source that proves or disproves it
7. **Other entities considered with verify links** — reviewer can click through to check rejected entities themselves
8. **Sources used** — audit trail of which tools were consulted
9. **No claim without a link** — if the system can't provide a source URL, it must not make the assertion

## Confidence Levels

| Level | Meaning |
|-------|---------|
| HIGH | Single clear entity, confirmed in official register, bidirectional evidence, active status, strong substance |
| MEDIUM | Entity confirmed but caveats exist (complex structure, thin capitalisation, unconfirmed status, one-directional evidence) |
| LOW | Entity identified but significant uncertainty (multiple plausible entities, weak evidence, partial confirmation only) |
| INSUFFICIENT | Cannot recommend — abstain. Evidence too weak, no registry match, or competing entities with no clear winner |

## When to return INSUFFICIENT (mandatory abstention)

- No registry match found for any candidate
- Multiple equally plausible entities with no differentiator
- Only company-controlled sources support the answer
- Entity found but status is dissolved/cancelled/voided
- Confidence below reviewer's decision threshold
