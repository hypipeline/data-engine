# Entity Lookup — Requirements

## Overview

Build an API endpoint that, given a company website URL, identifies the correct legal entity for M&A and ownership analysis.

**Priority: accuracy over completeness.** If not highly confident in the exact legal entity, return null rather than guessing.

---

## Entity Selection Rules

### General Rules

* **Listed companies**: return the publicly listed parent entity, verified against the relevant official company register
* **Private equity / investment firms / capital partners / asset managers**: return the exact legal entity explicitly identified in authoritative sources as the management company, investment adviser, manager, general partner, or operating entity
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
4. Other approved third-party authoritative source

Only use a lower-priority source if:
* the official company register is not available
* the official company register does not provide a sufficiently clear result
* the entity type is better identified through a regulator database (e.g. investment adviser or fund manager)

### Preferred First-Instance Sources (Examples)

* **Germany**: Unternehmensregister
* **UK**: Companies House
* **Canada**: federal or provincial corporate registries
* **US**: relevant Secretary of State or state business register
* **Luxembourg**: RCS
* **Spain**: relevant official commercial register or regulator

---

## Source Requirements (Strict)

### Allowed verification sources:
* Official national, federal, state, or provincial company registers
* SEC (EDGAR filings, registrant pages)
* SEC Investment Adviser Public Disclosure (IAPD)
* Financial regulators (FCA, FINRA, CNMV, BaFin-related official sources, etc.)
* Stock exchange filings
* Official federal or provincial corporate registries

### NOT allowed as primary verification sources:
* Company websites
* Marketing materials
* Press releases
* Any company-controlled content

Company-controlled sources may be used **only** to generate candidate legal names and infer jurisdiction. They may **not** be used to verify the final answer.

---

## Candidate-Name Extraction

If the input is a brand name, abbreviated name, or trade name, first search for candidate full legal entity names.

Company-controlled materials may be used **only** for extracting:
* a candidate legal entity name
* a likely jurisdiction
* a possible corporate suffix or formal naming convention

Sources: footer, copyright notice, terms of use, privacy policy, legal notice, contact/about pages.

Any candidate found this way **must be independently verified** in an approved third-party source.

---

## Jurisdiction-Led Search Process

1. Use neutral or company-controlled sources to infer likely jurisdiction
2. Use neutral or company-controlled sources to identify candidate legal entity names
3. Search the relevant official company register in that jurisdiction first
4. Verify whether an active or valid entity exists matching the candidate name exactly
5. If the official register doesn't resolve, use the relevant official regulator or filing database
6. If confirmed, return the exact registered name from the best available official source

---

## Private Equity / Investment Firm Rules

* Do not assume the input name is the legal entity
* Search for formal legal variants (e.g. "Management", "Advisors", "GP", etc.)
* Check the official company register first where possible
* If a regulator identifies a specific entity as the adviser/manager, return that exact entity
* Do not return fund names unless explicitly correct

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

### Regulatory Status Rule

If the source displays entity status, extract and return it (e.g. "Active", "Approved", "Registered", "Inactive", "Dissolved", "Terminated").

If multiple statuses are shown, return the current or most recent. If no status available, return null.

---

## Mandatory Abstention Rule

Return null if ANY of the following apply:
* Less than 90% confident
* Cannot verify using an approved third-party source
* Multiple plausible entities exist
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

If any answer is "no", return null.

---

## Output Format

```json
[
  {
    "input_name": "",
    "legal_entity_name": "",
    "source": "",
    "regulatory_status": ""
  }
]
```

If no match:
```json
[
  {
    "input_name": "",
    "legal_entity_name": null,
    "source": null,
    "regulatory_status": null
  }
]
```

---

## Key Principle

It is strictly better to return null than to return an incorrect legal entity.
