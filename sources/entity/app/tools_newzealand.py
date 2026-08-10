"""
New Zealand registry (Companies Office / NZBN) lookup — free via the NZ Companies Register's
public search service. No API key required.

Endpoint (GET, JSON):
    https://app.companiesoffice.govt.nz/companies/app/service/services/entity/search
        ?mode=standard&q=<name-or-number>

Each record carries: identifier (the NZ company number, e.g. 1585146), nzbn (13-digit New
Zealand Business Number), name, statusGroup (REGISTERED / REMOVED / …), entityType (LTD / …),
incorporationDate and entityRegisteredOffice. The company number (identifier) is the
registry_id the pipeline otherwise cannot obtain for New Zealand. Officers/financials are not
in this search response.

Like the Singapore (ACRA) mixin, the endpoint's q= is loose (word-overlap), so we rank
client-side by normalised-name similarity so the true match surfaces rather than a pile of
word-overlap hits.
"""
import re
import time

import requests

NZ_SEARCH_URL = "https://app.companiesoffice.govt.nz/companies/app/service/services/entity/search"
NZ_ENTITY_URL = "https://app.companiesoffice.govt.nz/companies/app/service/services/entity/"
# Single-page ("View as Single Page") company view — renders directors + shareholders + share
# allocations inline. Client-rendered, so fetched via Browserbase.
NZ_DETAIL_URL = "https://app.companiesoffice.govt.nz/companies/app/ui/pages/companies/{}/detail"
_NZ_UA = "Mozilla/5.0 (compatible; EntityLookup/1.0; +https://dataengine.hyndlandpartners.com)"

# Legal-form suffixes stripped when normalising names for comparison (kept in display).
_NZ_SUFFIXES = [
    "LIMITED PARTNERSHIP", "LIMITED", "LTD", "INCORPORATED", "INC",
    "LLC", "LP", "COMPANY", "CO",
]


def _nz_norm(s: str) -> str:
    """Normalise an entity name for matching: uppercase, drop punctuation, strip legal suffixes."""
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    s = re.sub(r"\s+", " ", s).strip()
    for suf in _NZ_SUFFIXES:
        s = re.sub(r"\b" + re.escape(suf) + r"\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class NewZealandMixin:
    def _nz_request(self, q: str):
        """GET the NZ Companies Office entity search with retry/backoff on transient failures."""
        last = None
        for attempt in range(3):
            self.count('nzco', op='search')
            try:
                r = requests.get(
                    NZ_SEARCH_URL,
                    params={"mode": "standard", "q": q},
                    headers={"Accept": "application/json", "User-Agent": _NZ_UA},
                    timeout=30,
                )
                if r.status_code == 200:
                    return r.json()
            except (requests.RequestException, ValueError) as e:  # noqa: BLE001
                last = e
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
        self.log.append({'tool': 'nzco', 'input': q, 'output': f'error/throttled: {last}'})
        return None

    @staticmethod
    def _nz_record(rec: dict) -> dict:
        return {
            "company_number": rec.get("identifier"),
            "nzbn": rec.get("nzbn"),
            "name": rec.get("name"),
            "status": rec.get("statusGroup"),
            "type": rec.get("entityType"),
            "incorporation_date": (rec.get("incorporationDate") or "")[:10] or None,
            "registered_office": rec.get("entityRegisteredOffice"),
            "source": "NZ Companies Office",
        }

    def _nz_detail(self, identifier: str) -> dict:
        """Fetch the entity detail record for extra ownership signal (director/shareholder counts,
        last-updated, previous names). Best-effort — returns {} on any failure. The full director /
        shareholder NAME lists are not exposed on this endpoint (see module notes)."""
        ident = str(identifier or "").strip()
        if not ident:
            return {}
        self.count('nzco', op='detail')
        try:
            r = requests.get(NZ_ENTITY_URL + ident,
                             headers={"Accept": "application/json", "User-Agent": _NZ_UA},
                             timeout=30)
            if r.status_code != 200:
                return {}
            d = r.json()
        except (requests.RequestException, ValueError):
            return {}
        prev = []
        for p in (d.get("previousNames") or []):
            nm = p.get("name") if isinstance(p, dict) else (p if isinstance(p, str) else None)
            if nm:
                prev.append(nm)
        return {
            "director_count": d.get("directorCount"),
            "shareholder_count": d.get("shareholderCount"),
            "last_updated": (d.get("lastUpdated") or "")[:10] or None,
            "previous_names": prev,
        }

    @staticmethod
    def _nz_parse_directors(html: str) -> list:
        """Parse the directors panel of the single-page view -> [(full_name, appointment_date), …]."""
        m = re.search(r'id="directorsPanel"(.*?)(?:id="shareholdersPanel"|id="\w+Panel"|</body>)', html, re.S)
        seg = m.group(1) if m else ""
        out = []
        for blk in re.split(r'class="director"', seg)[1:]:
            nm = re.search(r'Full legal name:</label>(.*?)</div>', blk, re.S)
            name = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', nm.group(1))).strip() if nm else ""
            if not name:
                continue
            ap = re.search(r'Appointment Date:</label>(.*?)</div>', blk, re.S)
            appt = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', ap.group(1))).strip() if ap else ""
            out.append((name, appt))
        return out

    @staticmethod
    def _nz_parse_shareholders(html: str):
        """Parse the shareholders panel -> (total_shares, [ {n, shares, pct, holders:[…]} , … ])."""
        m = re.search(r'id="shareholdersPanel"(.*?)(?:id="documentsPanel"|id="\w+Panel"|</body>)', html, re.S)
        seg = m.group(1) if m else ""
        tot = re.search(r'Total Number of Shares:\s*</label>\s*<span>([\d,]+)', seg)
        total_shares = tot.group(1) if tot else None
        am = re.search(r'id="allocations"(.*)', seg, re.S)
        aseg = am.group(1) if am else ""
        allocs = []
        for blk in re.split(r'class="allocationDetail"', aseg)[1:]:
            num = re.search(r'allocationNumber">(\d+)', blk)
            shares = re.search(r'name="shares"\s+value="(\d+)"', blk)
            pct = re.search(r'\(([\d.]+)%\)', blk)
            vals = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', v)).strip()
                    for v in re.findall(r'class="labelValue col2">(.*?)</div>', blk, re.S)]
            vals = [v for v in vals if v]
            holders = [vals[i] for i in range(0, len(vals), 2)]     # name, address, name, address …
            allocs.append({
                "n": num.group(1) if num else "?",
                "shares": shares.group(1) if shares else None,
                "pct": pct.group(1) if pct else None,
                "holders": holders,
            })
        return total_shares, allocs

    def newzealand_ownership(self, company_number: str) -> str:
        """Deep ownership fetch: renders the single-page company view via Browserbase and returns
        the directors (with appointment dates) and the shareholding allocations (shares, %, and the
        shareholder names). Returns '' if Browserbase is unavailable or the page can't be parsed."""
        ident = str(company_number or "").strip()
        if not ident:
            return ""
        self.count('nzco', op='ownership')
        html = self.browserbase_fetch_html(NZ_DETAIL_URL.format(ident))
        if not html or html.startswith("Error"):
            self.log.append({'tool': 'nzco_ownership', 'input': ident, 'output': 'browserbase unavailable/empty'})
            return ""
        directors = self._nz_parse_directors(html)
        total, allocs = self._nz_parse_shareholders(html)
        parts = []
        if directors:
            parts.append("Directors (" + str(len(directors)) + "): " + "; ".join(
                (n + (f" [appointed {a}]" if a else "")) for n, a in directors))
        if allocs:
            segs = []
            for al in allocs:
                head = f"Allocation {al['n']}"
                if al['shares']:
                    head += f" — {al['shares']} shares"
                if al['pct']:
                    head += f" ({al['pct']}%)"
                segs.append(head + ": " + ", ".join(al['holders']))
            parts.append("Shareholdings" + (f" (total {total} shares)" if total else "") + " — " + " | ".join(segs))
        out = "\n".join(parts)
        self.log.append({'tool': 'nzco_ownership', 'input': ident,
                         'output': f'{len(directors)} directors, {len(allocs)} allocation(s)'})
        return out

    def search_newzealand(self, entity_name: str) -> str:
        """Search the NZ Companies Register by name → ranked matches with company number (identifier),
        NZBN and status. Returns a formatted string for the analysis LLM (or a 'no results' line)."""
        name = (entity_name or "").strip()
        if not name:
            return "No New Zealand (Companies Office) results found."
        self._progress('registry', f'Searching NZ Companies Office for "{name}"...')

        data = self._nz_request(name)
        if data is None:
            out = "NZ Companies Office: no response (throttled/error — not a confirmed empty result)."
            self.log.append({'tool': 'nzco', 'input': name, 'output': out})
            return out

        records = [r for r in (data.get("list") or []) if r.get("category") == "entity" and r.get("name")]
        nq = _nz_norm(name)
        scored = []
        for rec in records:
            nn = _nz_norm(rec.get("name"))
            if not nn:
                continue
            if nn == nq:
                score = 100
            elif nq and (nq in nn or nn in nq):
                score = 80
            else:                                       # token overlap (Jaccard)
                a, b = set(nq.split()), set(nn.split())
                score = int(60 * len(a & b) / len(a | b)) if (a and b) else 0
            live = (rec.get("statusGroup") or "").upper() == "REGISTERED"
            scored.append((score, 1 if live else 0, rec))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        top = [r for r in scored if r[0] >= 40][:6]     # keep only plausible matches

        if not top:
            out = f'No NZ Companies Office match for "{name}".'
            self.log.append({'tool': 'nzco', 'input': name, 'output': f'{len(records)} raw, 0 ranked'})
            self._progress('registry', f'NZ Companies Office: 0 strong matches for "{name}"')
            return out

        # Enrich the single best match with directors/shareholders counts + previous names (one detail call).
        best_detail = self._nz_detail(top[0][2].get("identifier")) if top else {}

        # Deep ownership (directors + shareholders + allocations) for a confident, exact-name match —
        # one Browserbase render of the single-page view, at most once per lookup run.
        ownership = ""
        if (top and top[0][0] == 100
                and not getattr(self, "_nz_ownership_done", False)
                and self.config.get('browserbase_api_key') and self.config.get('browserbase_project_id')):
            self._nz_ownership_done = True
            ownership = self.newzealand_ownership(top[0][2].get("identifier"))

        lines = ["=== NZ Companies Office (Companies Register) Results ==="]
        for i, (_score, _live, rec) in enumerate(top):
            r = self._nz_record(rec)
            line = (
                f"{r['name']} | Company No: {r['company_number']} | NZBN: {r['nzbn']} | "
                f"Status: {r['status']} | Type: {r['type']}"
                + (f" | Incorporated: {r['incorporation_date']}" if r['incorporation_date'] else "")
                + (f" | {r['registered_office']}" if r['registered_office'] else "")
            )
            if i == 0 and best_detail:
                dc, sc = best_detail.get("director_count"), best_detail.get("shareholder_count")
                if dc is not None or sc is not None:
                    line += f" | Directors: {dc} | Shareholders: {sc}"
                if best_detail.get("previous_names"):
                    line += " | Prev names: " + "; ".join(best_detail["previous_names"][:3])
            lines.append(line)
        if ownership:
            lines.append("  " + ownership.replace("\n", "\n  "))
        out = "\n".join(lines)
        self.log.append({'tool': 'nzco', 'input': name, 'output': f'{len(top)} matches'})
        self._progress('registry', f'NZ Companies Office: {len(top)} match(es) for "{name}"')
        return out

    def lookup_newzealand_by_number(self, number: str):
        """Exact lookup by NZ company number (identifier) or NZBN, for validation. Returns a dict
        (name/status/type/company_number/nzbn/…) or None."""
        num = (number or "").strip()
        if not num:
            return None
        data = self._nz_request(num)
        for rec in ((data or {}).get("list") or []):
            if rec.get("category") != "entity":
                continue
            if str(rec.get("identifier")) == num or str(rec.get("nzbn")) == num:
                return self._nz_record(rec)
        return None
