# Entity Lookup v2 — Requirements

## Overview

Build an API endpoint that, given a company website URL, identifies the optimal legal entity for contracting, credit extension, and M&A analysis.

**Priority: accuracy over completeness.** If not highly confident in the exact legal entity, return null rather than guessing.

---

## Core Objectives

1. **Identify the best contracting entity** in the corporate structure (prefer TopCo / ultimate parent where possible)
2. **Score confidence** that credit can safely be extended to the recommended entity
3. **Prove the chain of evidence** linking the input URL to the recommended entity with verifiable source links

---

## Entity Selection Rules

### TopCo Preference Rule

Always prefer contracting with the highest viable entity in the corporate structure:

**Priority order:**
1. Ultimate parent / holding company (TopCo)
2. Publicly listed parent entity
3. Registered management company / investment adviser (for PE/asset managers)
4. Main operating company
5. Named subsidiary or operating entity (last resort)

**Rationale:** The highest entity in the structure typically has the broadest asset base, making it the strongest counterparty for credit extension.

**Constraints:**
* Only recommend a TopCo if the ownership link to the website entity is clearly verifiable
* If the corporate structure is opaque or the TopCo cannot be confirmed, fall back to the entity directly confirmed in the registry
* Never infer a parent entity that isn't explicitly documented in an authoritative source
* Flag when the recommended entity differs from the entity directly associated with the website (e.g. "Website operates as X; recommended contracting entity is parent Y")

### General Rules

* **Listed companies**: return the publicly listed parent entity, verified against the relevant official company register
* **Private equity / investment firms / capital partners / asset managers**: return the exact legal entity identified as the management company, investment adviser, manager, general partner, or operating entity — then attempt to identify its parent/TopCo
* **Strategic acquirers**: return the main operating or holding company
* **Brands, subsidiaries, or trade names**: map to the correct parent legal entity only if clearly verifiable

### Critical Entity-Selection Rule

Return the exact legal entity supported by the source, not a nearby, affiliated, or similarly named entity.

If the source identifies one entity as the registrant, filer, adviser, manager, operator, or active registered company, return that exact entity — not a related one.

---

## Source Priority (Critical)

Use official company registers in the first instance wherever possible.

**Priority order:**

1. Official national, federal, state, or provincial company register
2. Official financial regulator database
3. Official stock exchange filing or securities filing source
4. Other approved third-party authoritative source (e.g. North Data)

Only use a lower-priority source if:
* the official company register is not available
* the official company register does not provide a sufficiently clear result
* the entity type is better identified through a regulator database (e.g. investment adviser or fund manager)

### Preferred First-Instance Sources (Examples)

* **Germany**: Unternehmensregister / North Data
* **UK**: Companies House
* **Canada**: federal or provincial corporate registries
* **US**: relevant Secretary of State or state business register
* **Luxembourg**: RCS / North Data
* **Spain**: relevant official commercial register or regulator / North Data

---

## Source Requirements (Strict)

### Allowed verification sources:
* Official national, federal, state, or provincial company registers
* SEC (EDGAR filings, registrant pages)
* SEC Investment Adviser Public Disclosure (IAPD)
* Financial regulators (FCA, FINRA, CNMV, BaFin-related official sources, etc.)
* Stock exchange filings
* Official federal or provincial corporate registries
* Approved aggregators (North Data for European jurisdictions)

### NOT allowed as primary verification sources:
* Company websites
* Marketing materials
* Press releases
* Any company-controlled content

Company-controlled sources may be used **only** to generate candidate legal names and infer jurisdiction. They may **not** be used to verify the final answer.

### Data Source Field Reliability (Critical)

Aggregator sources (e.g. North Data) combine data from multiple underlying sources. Not all fields within a single source are equally reliable.

**North Data — field reliability:**

| Field | Underlying Source | Reliability |
|-------|------------------|-------------|
| Entity name | Official commercial register | HIGH — treat as authoritative |
| Registry ID (RCS, HRB, KVK, etc.) | Official commercial register | HIGH |
| Registered address | Official commercial register | HIGH |
| Directors / officers | Official commercial register / gazette | HIGH |
| Beneficial owners | Official register (where mandatory) | HIGH |
| Ownership percentages | Official filings | HIGH |
| Financial data | Official annual filings | MEDIUM — may be estimated |
| Website URL | User-submitted or web-scraped | LOW — must be independently verified |
| Email address | User-submitted or web-scraped | LOW — must be independently verified |
| Segment classification | Algorithmic | LOW |

**Rule: Never use a LOW-reliability field as evidence in the report without independent verification.** A website URL shown on North Data is NOT proof that the entity operates that website. It must be corroborated by WHOIS, SEC filings, or other authoritative sources.

---

## Candidate-Name Extraction

If the input is a brand name, abbreviated name, or trade name, first search for candidate full legal entity names.

Company-controlled materials may be used **only** for extracting:
* a candidate legal entity name
* a likely jurisdiction
* a possible corporate suffix or formal naming convention

Sources: footer, copyright notice, terms of use, privacy policy, legal notice, contact/about pages, WHOIS registrant data.

Any candidate found this way **must be independently verified** in an approved third-party source.

---

## Jurisdiction-Led Search Process

1. Use neutral or company-controlled sources to infer likely jurisdiction
2. Use neutral or company-controlled sources to identify candidate legal entity names
3. Search the relevant official company register in that jurisdiction first
4. Verify whether an active or valid entity exists matching the candidate name exactly
5. If the official register doesn't resolve, use the relevant official regulator or filing database
6. If confirmed, return the exact registered name from the best available official source
7. Attempt to identify the parent/TopCo of the confirmed entity using corporate structure data from the registry or regulator

---

## Private Equity / Investment Firm Rules

* Do not assume the input name is the legal entity
* Search for formal legal variants (e.g. "Management", "Advisors", "GP", etc.)
* Check the official company register first where possible
* If a regulator identifies a specific entity as the adviser/manager, return that exact entity
* Do not return fund names unless explicitly correct
* Attempt to identify the TopCo/parent of the management entity

If both a company register and a regulator source are available:
* Prefer the company register for the exact legal entity name
* Use the regulator source to identify regulatory role or status

---

## Matching Rules

### Corporate-Register Match Rule

Return an entity ONLY if:
* It appears in an official register, regulator filing, or securities filing
* The returned name exactly matches the legal entity in that source
* It clearly corresponds to the input company
* There is no competing equally plausible entity
* Any website-derived candidate name has been independently confirmed

### Exact String Match Rule (Critical)

Return the legal entity name **exactly** as it appears in the authoritative source.
* Do not add or remove words
* Do not normalize or standardize names
* Do not expand or shorten the entity name
* Do not modify punctuation, spacing, capitalization, or suffixes
* Character-for-character match to the source

### Name Change / Conversion Rule (Critical)

If the report claims an entity was renamed, converted (e.g. L.P. → Inc.), or succeeded by another entity:
* The claim must cite a specific source showing the name change (e.g. SEC former names field, registry amendment filing, gazette notice)
* The exact former name and exact current name must be stated as they appear in the source — not paraphrased
* The date of the change must be included if available in the source
* A clickable source link must be provided
* If the former name in one source (e.g. SEC) differs from the name in another source (e.g. state registry), this discrepancy must be flagged — do not silently assume they are the same entity

### Regulatory Status Rule

If the source displays entity status, extract and return it (e.g. "Active", "Approved", "Registered", "Inactive", "Dissolved", "Terminated").

If multiple statuses are shown, return the current or most recent. If no status available, return null.

---

## Credit Confidence Score

Every result must include a `credit_confidence` object scoring how suitable this entity is as a credit counterparty.

### Score Components

| Factor | Weight | Description |
|--------|--------|-------------|
| `entity_confirmed` | 30% | Entity verified in official register (not just inferred) |
| `status_active` | 20% | Entity has active/current status in register |
| `jurisdiction_quality` | 15% | Jurisdiction has strong legal framework and enforcement |
| `structure_visibility` | 15% | Corporate structure is visible (parent/subs identifiable) |
| `identity_match` | 10% | Strength of bidirectional link between website and entity (forward + reverse evidence) |
| `entity_age` | 10% | Entity has meaningful operating history |

### Score Bands

| Band | Score | Meaning |
|------|-------|---------|
| `high` | 80-100 | Strong counterparty. Entity confirmed, active, in a well-regulated jurisdiction with visible corporate structure. |
| `medium` | 50-79 | Acceptable with caveats. Entity confirmed but some factors uncertain (e.g. opaque structure, weaker jurisdiction). |
| `low` | 20-49 | Caution advised. Entity only partially confirmed, or significant concerns on one or more factors. |
| `insufficient` | 0-19 | Do not extend credit. Entity unconfirmed, inactive, or in a jurisdiction with poor enforcement. |

### Jurisdiction Quality Tiers

* **Tier 1** (score: 100): US, UK, Germany, France, Netherlands, Switzerland, Canada, Australia, Japan, Singapore, Hong Kong
* **Tier 2** (score: 75): Spain, Italy, Ireland, Belgium, Luxembourg, Austria, Denmark, Sweden, Norway, Finland, Israel, South Korea
* **Tier 3** (score: 50): Poland, Czech Republic, Portugal, Greece, Romania, Croatia, Estonia, Malta, Cyprus, UAE, India, Brazil, Mexico
* **Tier 4** (score: 25): Other jurisdictions with limited transparency or enforcement

---

## Provable Bridge (Evidence Chain)

Every result must include an `evidence_chain` array documenting the verifiable path from the input URL to the recommended entity. Each step must include a clickable source link where possible.

### Required Evidence Steps

1. **URL → Candidate Name**: How the candidate entity name was extracted
   - Source: WHOIS record, website footer, copyright notice, terms page, etc.
   - Link: the specific page URL or WHOIS lookup URL

2. **Candidate → Registry Match**: How the candidate was verified in an official source
   - Source: company register, SEC filing, regulator database
   - Link: direct URL to the registry entry, filing, or search result

3. **Registry Entity → Recommended Entity**: How the final recommendation was derived
   - If TopCo: link showing the parent/subsidiary relationship
   - If same entity: state that the registry entity is the recommended entity
   - Link: corporate structure page, annual filing, or parent company registry entry

### Reverse Evidence (Entity → URL)

In addition to the forward chain (URL → Entity), every result must include reverse evidence proving the recommended entity links back to the input URL. This creates a bidirectional proof — the forward chain shows how we found the entity, the reverse chain confirms the entity actually controls or operates the website.

**If reverse evidence cannot be established, the recommendation confidence must drop to LOW or INSUFFICIENT.** Forward evidence alone is never sufficient for a HIGH or MEDIUM recommendation.

**Reverse validation methods (three pillars):**

#### 1. Name Matching
The entity's registered name must appear on the website, or the website's stated operator must appear in the entity's registry records.
* **Exact or near-exact** name correspondence required — not just a shared word
* "GEM Global Yield LLC SCS" appearing verbatim on gemny.com = strong
* Both contain "GEM" but no exact match = not evidence
* Strength: `strong` if exact match, `weak` if partial only

#### 2. Address Matching
The entity's registered office address must match an address on the website (contact page, footer, terms, about page), or the WHOIS registrant address must match the registry address.
* Must be a specific address match, not just same city
* "28 Cours Albert 1er, 75008 Paris" on both = strong
* "New York, NY" on both = weak (too generic)
* Strength: `strong` if specific address match, `weak` if city-level only

#### 3. People Matching
Named individuals on the website (team page, about page, leadership) must match directors, officers, or beneficial owners in registry records.
* "Christopher Brown, Managing Director" on website + Christopher Brown listed as MD in Luxembourg RCS = strong
* Must be the same person, not just same name — corroborate with role, location, or other details
* Strength: `strong` if name + role match, `moderate` if name only

**Additional reverse evidence sources:**

4. **WHOIS registrant match** — the domain's WHOIS registrant org matches the recommended entity or a known subsidiary
   - Link: WHOIS lookup URL
   - Strength: `strong` (direct domain ownership proof)

5. **SEC/regulator filing references the domain** — an EDGAR filing, Form ADV, or regulator entry lists the website URL
   - Link: filing URL showing the domain reference
   - Strength: `strong` (official filing confirms website)

6. **Domain name matches entity name** — the domain is a clear derivative of the entity name (e.g. `acmecorp.com` → `Acme Corporation`)
   - Link: n/a (observation)
   - Strength: `weak` (suggestive only, not proof of ownership)

**Scoring:**
* 2+ strong reverse signals → `identity_match`: `bidirectional` (full score, HIGH eligible)
* 1 strong or 2+ moderate/weak → `identity_match`: `partial` (MEDIUM eligible)
* Forward evidence only, no reverse → `identity_match`: `forward_only` (LOW maximum)
* Contradictory reverse evidence → `identity_match`: `conflicting` (INSUFFICIENT)

### Evidence Quality Flags

Each evidence step (forward or reverse) should be flagged:
* `verified` — confirmed via authoritative third-party source with direct link
* `inferred` — derived from company-controlled source only (not independently verified)
* `unavailable` — source exists but link could not be retrieved programmatically

---

## Mandatory Abstention Rule

Return null if ANY of the following apply:
* Less than 90% confident in the entity identification
* Cannot verify using an approved third-party source
* Multiple plausible entities exist with no clear winner
* Result would require inference rather than confirmation
* Only company-controlled sources support the answer
* Cannot match the entity name exactly to the third-party source
* Candidate name was not confirmed in the relevant official register
* An official company register was available but was not checked first

**Do not guess under any circumstances.**

---

## Validation Checklist (Before Answering)

* Is this entity explicitly named in a third-party authoritative source?
* Did you check the official company register first where one was available?
* Does the output exactly match the source string character-for-character?
* Is it the registrant / legal entity rather than a brand?
* If the input was a brand or short name, did you first identify a candidate legal name and then verify it in an official register?
* Is there any similarly named entity that could be correct instead?
* Does this require inference beyond the source?
* Have you attempted to identify the TopCo / ultimate parent?
* Is each evidence step documented with a source link?
* Has the credit confidence score been calculated?

If any of the first seven answers is "no", return null.

---

## Output Format

```json
[
  {
    "input_url": "https://example.com",
    "website_entity": {
      "legal_entity_name": "Example Operating Company LLC",
      "source": "Delaware Division of Corporations",
      "source_url": "https://icis.corp.delaware.gov/ecorp/...",
      "regulatory_status": "Active",
      "jurisdiction": "US-DE",
      "registry_id": "1234567"
    },
    "recommended_entity": {
      "legal_entity_name": "Example Holdings Inc.",
      "source": "SEC EDGAR",
      "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=...",
      "regulatory_status": "Active",
      "jurisdiction": "US-DE",
      "registry_id": "7654321",
      "relationship_to_website_entity": "Ultimate parent / holding company"
    },
    "credit_confidence": {
      "score": 85,
      "band": "high",
      "factors": {
        "entity_confirmed": true,
        "status_active": true,
        "jurisdiction_quality": "tier_1",
        "structure_visibility": "parent_identified",
        "identity_match": "bidirectional",
        "entity_age": "5+ years"
      },
      "flags": []
    },
    "evidence_chain": {
      "forward": [
        {
          "step": "url_to_candidate",
          "description": "WHOIS registrant organization: Example Operating Company LLC",
          "source": "WHOIS",
          "source_url": "https://who.is/whois/example.com",
          "quality": "verified"
        },
        {
          "step": "candidate_to_registry",
          "description": "Confirmed in Delaware Division of Corporations as file 1234567, status Active",
          "source": "Delaware Division of Corporations",
          "source_url": "https://icis.corp.delaware.gov/ecorp/...",
          "quality": "verified"
        },
        {
          "step": "registry_to_recommendation",
          "description": "SEC EDGAR 10-K filing lists Example Holdings Inc. as parent company of Example Operating Company LLC",
          "source": "SEC EDGAR",
          "source_url": "https://www.sec.gov/...",
          "quality": "verified"
        }
      ],
      "reverse": [
        {
          "step": "entity_to_domain_whois",
          "description": "WHOIS registrant org 'Example Operating Company LLC' matches confirmed website entity",
          "source": "WHOIS",
          "source_url": "https://who.is/whois/example.com",
          "strength": "strong",
          "quality": "verified"
        },
        {
          "step": "entity_to_domain_filing",
          "description": "SEC Form 10-K for Example Holdings Inc. lists www.example.com as company website",
          "source": "SEC EDGAR",
          "source_url": "https://www.sec.gov/...",
          "strength": "strong",
          "quality": "verified"
        },
        {
          "step": "topco_to_subsidiary",
          "description": "Example Holdings Inc. 10-K Exhibit 21 lists Example Operating Company LLC as wholly-owned subsidiary",
          "source": "SEC EDGAR",
          "source_url": "https://www.sec.gov/...",
          "strength": "strong",
          "quality": "verified"
        }
      ]
    }
  }
]
```

If no match:
```json
[
  {
    "input_url": "https://example.com",
    "website_entity": null,
    "recommended_entity": null,
    "credit_confidence": {
      "score": 0,
      "band": "insufficient",
      "factors": {},
      "flags": ["no_registry_match_found"]
    },
    "evidence_chain": {
      "forward": [
        {
          "step": "url_to_candidate",
          "description": "WHOIS registrant redacted by privacy proxy",
          "source": "WHOIS",
          "source_url": "https://who.is/whois/example.com",
          "quality": "unavailable"
        }
      ],
      "reverse": []
    }
  }
]
```

---

## Key Principles

1. It is strictly better to return null than to return an incorrect legal entity.
2. Always prefer the TopCo where the ownership link is verifiable.
3. Every recommendation must be backed by a clickable evidence chain.
4. The credit confidence score must reflect reality — never inflate confidence to avoid a null result.
