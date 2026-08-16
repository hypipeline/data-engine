"""Title -> seniority classifier for free-text LinkedIn `position` strings.

Mirrors Crunchbase's `levels` taxonomy but computed locally, so we get structured
seniority over a FULL company roster (not capped). Rules-only (free, deterministic);
an LLM cleanup pass over ambiguous titles can be layered later.

classify(position) -> {"level": one of LEVELS, "decision_maker": bool, "primary": str}
"""
import re

LEVELS = ["exec", "vp", "director", "manager", "ic", "unknown"]
DECISION_LEVELS = {"exec", "vp", "director"}

# "partner" appears in many non-executive roles — exclude these so PE "Partner" ≠ "Business Partner"
_PARTNER_NEG = [
    "business partner", "solution partner", "channel partner", "sales partner",
    "technology partner", "strategic partner", "implementation partner", "delivery partner",
    "partner success", "partner manager", "partner engineer", "partner solution",
    "partner account", "alliance partner", "referral partner", "partner development",
    "procurement business partner", "hr business partner", "people partner", "partner marketing",
]

_EXEC_PAT = [
    r"\bce[o0]\b", r"\bcfo\b", r"\bcoo\b", r"\bcto\b", r"\bcmo\b", r"\bcio\b", r"\bcro\b",
    r"\bcpo\b", r"\bcco\b", r"\bchief\b", r"\bfounder\b", r"\bco[- ]?founder\b", r"\bowner\b",
    r"\bproprietor\b", r"\bchair(man|woman|person)?\b", r"\bboard member\b", r"\bboard of directors\b",
    r"\bmanaging (partner|director|member)\b", r"\bgeneral partner\b", r"\bprincipal\b",
]


def _primary_role(pos: str) -> str:
    """Take the primary current role — the first segment before a pipe/bullet/' - ' — so that
    side-projects ('...| Co-founder of X') don't inflate seniority."""
    return re.split(r"[|•·\n]| - ", str(pos))[0].strip()


def classify(position):
    if not position or not str(position).strip() or str(position).strip() in ("-", "--"):
        return {"level": "unknown", "decision_maker": False, "primary": ""}
    primary = _primary_role(position)
    t = " " + primary.lower() + " "

    # hard demotion: assistants/coordinators/interns are ICs even if the line mentions a CxO
    if re.search(r"\b(assistant to|executive assistant|assistant|coordinator|intern|apprentice)\b", t):
        return {"level": "ic", "decision_maker": False, "primary": primary}

    has_vp = bool(re.search(r"\b(vice[- ]president|svp|evp|vp)\b", t))
    partner_ok = bool(re.search(r"\bpartner\b", t)) and not any(n in t for n in _PARTNER_NEG)

    if re.search(r"\bpresident\b", t) and not has_vp:
        return {"level": "exec", "decision_maker": True, "primary": primary}
    if partner_ok or any(re.search(p, t) for p in _EXEC_PAT):
        return {"level": "exec", "decision_maker": True, "primary": primary}
    if has_vp:
        return {"level": "vp", "decision_maker": True, "primary": primary}
    if re.search(r"\bdirector\b", t) or re.search(r"\bhead of\b", t):
        return {"level": "director", "decision_maker": True, "primary": primary}
    if re.search(r"\b(manager|lead|supervisor)\b", t):
        return {"level": "manager", "decision_maker": False, "primary": primary}
    return {"level": "ic", "decision_maker": False, "primary": primary}
