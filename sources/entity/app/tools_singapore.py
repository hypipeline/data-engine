"""
Singapore registry (ACRA) lookup — free via data.gov.sg's open "Entities Registered with
ACRA" dataset (~1.5M active + inactive entities, monthly refresh, Open Data Licence).

The datastore API's global `q=` is loose (matches any word), so we query the field-scoped
form q={"entity_name": ...} and then RANK client-side by normalised-name similarity — the same
approach used for Bizapedia/GLEIF — so the true match surfaces rather than a word-overlap pile.

Fields available per record: uen, entity_name, uen_status_desc (status), entity_type_desc,
uen_issue_date, reg_street_name, reg_postal_code. The UEN is Singapore's authoritative
registry id (e.g. 200904830K) — exactly the registry_id the pipeline otherwise can't get for
Singapore. Officers/financials are NOT in the free dataset (those need ACRA's paid EIQ/FIQ API).
"""
import json
import re
import time

import requests

ACRA_URL = "https://data.gov.sg/api/action/datastore_search"
ACRA_RESOURCE_ID = "d_3f960c10fed6145404ca7b821f263b87"     # Entities Registered with ACRA

# Legal-form suffixes stripped when normalising names for comparison (kept in display).
_SG_SUFFIXES = [
    "PRIVATE LIMITED", "PTE LTD", "PTE LIMITED", "LIMITED LIABILITY PARTNERSHIP",
    "LIMITED PARTNERSHIP", "LLP", "LLC", "LP", "PTE", "LTD", "LIMITED", "INC", "INCORPORATED",
]


def _sg_norm(s: str) -> str:
    """Normalise an entity name for matching: uppercase, drop punctuation, strip legal suffixes."""
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    s = re.sub(r"\s+", " ", s).strip()
    for suf in _SG_SUFFIXES:
        s = re.sub(r"\b" + re.escape(suf) + r"\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class SingaporeMixin:
    def _acra_request(self, params):
        """GET the data.gov.sg datastore with retry/backoff on transient failures."""
        last = None
        for attempt in range(3):
            self.api_calls['acra'] = self.api_calls.get('acra', 0) + 1
            try:
                r = requests.get(ACRA_URL, params=params, timeout=30)
                if r.status_code == 200:
                    d = r.json()
                    if d.get("success"):
                        return d.get("result") or {}
            except requests.RequestException as e:  # noqa: BLE001
                last = e
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
        self.log.append({'tool': 'acra', 'input': str(params.get('q')), 'output': f'error/throttled: {last}'})
        return None

    def search_singapore(self, entity_name: str) -> str:
        """Search ACRA (Singapore) by name → ranked matches with UEN + status. Returns a formatted
        string for the analysis LLM (or a 'no results' line)."""
        name = (entity_name or "").strip()
        if not name:
            return "No Singapore (ACRA) results found."
        self._progress('registry', f'Searching ACRA (Singapore) for "{name}"...')

        # field-scoped full-text on entity_name (far tighter than the global q=)
        core = _sg_norm(name) or name
        result = self._acra_request({
            "resource_id": ACRA_RESOURCE_ID,
            "q": json.dumps({"entity_name": core}),
            "limit": 50,
        })
        if result is None:
            out = "ACRA: no response (throttled/error — not a confirmed empty result)."
            self.log.append({'tool': 'acra', 'input': name, 'output': out})
            return out

        records = result.get("records") or []
        nq = _sg_norm(name)
        scored = []
        for rec in records:
            nn = _sg_norm(rec.get("entity_name"))
            if not nn:
                continue
            if nn == nq:
                score = 100
            elif nq and (nq in nn or nn in nq):
                score = 80
            else:                                       # token overlap (Jaccard)
                a, b = set(nq.split()), set(nn.split())
                score = int(60 * len(a & b) / len(a | b)) if (a and b) else 0
            live = (rec.get("uen_status_desc") or "").lower() in ("registered", "live", "existing")
            scored.append((score, 1 if live else 0, rec))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        top = [r for r in scored if r[0] >= 40][:6]     # keep only plausible matches

        if not top:
            out = f'No ACRA (Singapore) match for "{name}".'
            self.log.append({'tool': 'acra', 'input': name, 'output': f'{len(records)} raw, 0 ranked'})
            self._progress('registry', f'ACRA (Singapore): 0 strong matches for "{name}"')
            return out

        lines = ["=== ACRA (Singapore) Registry Results ==="]
        for score, _live, rec in top:
            addr = ", ".join(x for x in [rec.get("reg_street_name"), rec.get("reg_postal_code")] if x)
            lines.append(
                f"{rec.get('entity_name')} | UEN: {rec.get('uen')} | "
                f"Status: {rec.get('uen_status_desc')} | Type: {rec.get('entity_type_desc')}"
                + (f" | Registered: {rec.get('uen_issue_date')}" if rec.get('uen_issue_date') else "")
                + (f" | {addr}" if addr else "")
            )
        out = "\n".join(lines)
        self.log.append({'tool': 'acra', 'input': name, 'output': f'{len(top)} matches'})
        self._progress('registry', f'ACRA (Singapore): {len(top)} match(es) for "{name}"')
        return out

    def lookup_singapore_by_uen(self, uen: str):
        """Exact UEN lookup for validation. Returns a dict (name/status/type/…) or None."""
        uen = (uen or "").strip().upper()
        if not uen:
            return None
        result = self._acra_request({
            "resource_id": ACRA_RESOURCE_ID,
            "filters": json.dumps({"uen": uen}),
            "limit": 1,
        })
        recs = (result or {}).get("records") or []
        if not recs:
            return None
        rec = recs[0]
        return {
            "uen": rec.get("uen"),
            "name": rec.get("entity_name"),
            "status": rec.get("uen_status_desc"),
            "type": rec.get("entity_type_desc"),
            "registration_date": rec.get("uen_issue_date"),
            "source": "ACRA (data.gov.sg)",
        }
