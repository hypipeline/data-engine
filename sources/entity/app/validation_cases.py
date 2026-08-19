"""Registry-validation LABEL cases — DB-backed & editable.

These are NOT pipeline runs. Each case stores an entity + the registry facts that were (or would
be) found, and the tool at /entity/tools/validation-labels renders the resulting VALIDATION BADGE
purely from the stored fields. Purpose: review/curate the validation-label scheme (which registry
outcome maps to which badge/confidence), so the labels on the entity card stay coherent.

Mirrors northdata_cases.py's storage pattern: _conn/enabled/ensure_schema/_seed_*_if_empty +
list/get/add/update/delete, RealDictCursor, the `entity.` schema, closing(_conn()).
"""
import os
from contextlib import closing

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None

_SEED = [
    {"name": "SREP Capital Management, LLC", "jurisdiction": "US · Delaware", "registry_id": "4334492",
     "source": "Delaware Div. of Corps.", "registry_status": "Active",
     "status": "verified", "confidence": "high",
     "note": "Name + ID match an active registry record → fully verified."},
    {"name": "Bluepeak Capital LLP", "jurisdiction": "GB", "registry_id": None,
     "source": "Companies House", "registry_status": "Active",
     "status": "name_verified", "confidence": "medium",
     "note": "Companies House returns this name as an ACTIVE record but no ID anchored (would usually recover OC428590 → verified)."},
    {"name": "Renaissance Services SAOG", "jurisdiction": "OM (Oman)", "registry_id": None,
     "source": "— (no coverage)", "registry_status": "—",
     "status": "no_registry_access", "confidence": "keep",
     "note": "No Oman registry integrated — the name was never checked. May be real, but the system cannot confirm; user must verify manually. Not a failure and not a claim of verification."},
    {"name": "Quest Global Services Pte. Ltd.", "jurisdiction": "SG", "registry_id": "—",
     "source": "ACRA", "registry_status": "Dissolved – s212(1)(d)",
     "status": "name_match_bad_status", "confidence": "low",
     "note": "Name matches but ACRA shows it wound up and dissolved — a match to a dead entity is not a verification."},
    {"name": "Example Holdings Ltd", "jurisdiction": "GB", "registry_id": "12345678",
     "source": "Companies House", "registry_status": "Liquidation",
     "status": "name_match_bad_status", "confidence": "low",
     "note": "Matched but in liquidation — treated as not-live."},
    {"name": "Old Trading Company LLC", "jurisdiction": "US · Delaware", "registry_id": "9999999",
     "source": "Delaware Div. of Corps.", "registry_status": "Void / Struck Off",
     "status": "name_match_bad_status", "confidence": "low",
     "note": "Registry record exists but has been voided / struck off."},
    {"name": "SREP Capital Management, LLC", "jurisdiction": "US · Delaware", "registry_id": "4334492",
     "source": "Bizapedia", "registry_status": "registry has 'SDA Capital Management, LLC'",
     "status": "name_mismatch", "confidence": "low",
     "note": "The ID resolves to a DIFFERENT name — recommendation doesn't line up."},
    {"name": "Nonexistent Widgets Inc", "jurisdiction": "US · Delaware", "registry_id": "—",
     "source": "Delaware Div. of Corps.", "registry_status": "—",
     "status": "not_found", "confidence": "low",
     "note": "Searched a registry we hold and the name is not there — different from 'no access'."},
]

_DSN = os.environ.get("DATABASE_URL")


def enabled() -> bool:
    return bool(_DSN and psycopg2)


def _conn():
    return psycopg2.connect(_DSN)


def ensure_schema() -> None:
    if not enabled():
        return
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE SCHEMA IF NOT EXISTS entity;
                CREATE TABLE IF NOT EXISTS entity.validation_label_cases (
                    id              bigserial PRIMARY KEY,
                    name            text NOT NULL,
                    jurisdiction    text,
                    registry_id     text,
                    source          text,
                    registry_status text,
                    status          text NOT NULL,
                    confidence      text,
                    note            text
                );
            """)
        c.commit()
    _seed_if_empty()


def _seed_if_empty():
    try:
        if not list_cases():
            for c in _SEED:
                add_case(c)
    except Exception as e:  # noqa: BLE001
        print(f"[validation_labels] seed skipped: {e}")


def _row(r):
    return {"id": r["id"], "name": r["name"], "jurisdiction": r.get("jurisdiction"),
            "registry_id": r.get("registry_id"), "source": r.get("source"),
            "registry_status": r.get("registry_status"), "status": r.get("status"),
            "confidence": r.get("confidence"), "note": r.get("note")}


def list_cases() -> list:
    if not enabled():
        return []
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM entity.validation_label_cases ORDER BY id")
            return [_row(r) for r in cur.fetchall()]


def get_case(cid: int):
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM entity.validation_label_cases WHERE id=%s", (cid,))
            r = cur.fetchone()
            return _row(r) if r else None


def _case_fields(case: dict):
    return (case.get("name") or "unnamed",
            case.get("jurisdiction"),
            case.get("registry_id"),
            case.get("source"),
            case.get("registry_status"),
            case.get("status") or "not_found",
            case.get("confidence"),
            case.get("note"))


def add_case(case: dict) -> int:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.validation_label_cases "
                "(name, jurisdiction, registry_id, source, registry_status, status, confidence, note) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id", _case_fields(case))
            cid = cur.fetchone()[0]
        c.commit()
    return cid


def update_case(cid: int, case: dict) -> None:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE entity.validation_label_cases SET name=%s, jurisdiction=%s, registry_id=%s, "
                "source=%s, registry_status=%s, status=%s, confidence=%s, note=%s WHERE id=%s",
                _case_fields(case) + (cid,))
        c.commit()


def delete_case(cid: int) -> None:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM entity.validation_label_cases WHERE id=%s", (cid,))
        c.commit()
