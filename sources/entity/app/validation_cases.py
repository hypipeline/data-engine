"""Registry-validation TEST cases — DB-backed & editable, real test harness.

Each case is an entity (name + jurisdiction + optional registry ID) plus an EXPECTED validation
status. Running a case instantiates the REAL EntityLookup agent and calls the production
ValidationMixin.validate_entity_in_registry(report) — which makes LIVE registry calls (Delaware via
Browserbase, NorthData, Companies House, ACRA, NZ) — then grades the derived actual status against
the expected one (pass/fail). Surfaced at /entity/tools/validation-labels.

Mirrors northdata_cases.py's storage pattern: _conn/enabled/ensure_schema/_seed_*_if_empty +
list/get/add/update/delete, RealDictCursor, the `entity.` schema, closing(_conn()), and the
last_result/last_run_at result-store pattern of run_resolution_case.
"""
import json
import os
from contextlib import closing

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover
    psycopg2 = None

# Real cases: INPUTS + an EXPECTED status. A Run executes the live validator and grades actual vs
# expected. Statuses the validator can produce: verified, name_match_bad_status, name_mismatch,
# not_found, no_registry_access (none → unvalidated); name_verified is a not-yet-built verifier.
_SEED = [
    {"name": "SREP Capital Management, LLC", "jurisdiction_country": "US", "jurisdiction_state": "DE",
     "registry_id": "4334492", "expect_status": "verified",
     "note": "Delaware active, ID-anchored — should verify."},
    {"name": "BluePeak Private Capital GP", "jurisdiction_country": "LU", "jurisdiction_state": None,
     "registry_id": "B248881", "expect_status": "verified",
     "note": "NorthData Luxembourg active."},
    {"name": "Renaissance Services SAOG", "jurisdiction_country": "OM", "jurisdiction_state": None,
     "registry_id": None, "expect_status": "no_registry_access",
     "note": "Oman — no registry integrated → manual verification required."},
    {"name": "Bluepeak Capital LLP", "jurisdiction_country": "GB", "jurisdiction_state": None,
     "registry_id": None, "expect_status": "name_verified",
     "note": "Active UK LLP with no ID. EXPECTED TO FAIL for now — the name_verified verifier isn't "
             "built yet; this case documents the gap and will go green once it is."},
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
                CREATE TABLE IF NOT EXISTS entity.validation_test_cases (
                    id                  bigserial PRIMARY KEY,
                    name                text NOT NULL,
                    jurisdiction_country text,
                    jurisdiction_state  text,
                    registry_id         text,
                    expect_status       text NOT NULL,
                    note                text,
                    last_result         jsonb,
                    last_run_at         timestamptz
                );
                -- one-time cleanup of the old render-only mock table (the new table persists user
                -- edits via seed-only-if-empty).
                DROP TABLE IF EXISTS entity.validation_label_cases;
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
    return {"id": r["id"], "name": r["name"],
            "jurisdiction_country": r.get("jurisdiction_country"),
            "jurisdiction_state": r.get("jurisdiction_state"),
            "registry_id": r.get("registry_id"),
            "expect_status": r.get("expect_status"),
            "note": r.get("note"),
            "last_result": r.get("last_result"),
            "last_run_at": r["last_run_at"].isoformat() if r.get("last_run_at") else None}


def list_cases() -> list:
    if not enabled():
        return []
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM entity.validation_test_cases ORDER BY id")
            return [_row(r) for r in cur.fetchall()]


def get_case(cid: int):
    if not enabled():
        return None
    with closing(_conn()) as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM entity.validation_test_cases WHERE id=%s", (cid,))
            r = cur.fetchone()
            return _row(r) if r else None


def _case_fields(case: dict):
    return (case.get("name") or "unnamed",
            case.get("jurisdiction_country") or None,
            case.get("jurisdiction_state") or None,
            case.get("registry_id") or None,
            case.get("expect_status") or "verified",
            case.get("note"))


def add_case(case: dict) -> int:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO entity.validation_test_cases "
                "(name, jurisdiction_country, jurisdiction_state, registry_id, expect_status, note) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", _case_fields(case))
            cid = cur.fetchone()[0]
        c.commit()
    return cid


def update_case(cid: int, case: dict) -> None:
    """Overwrite a case in place — editing changes what pass/fail means, so clear the last result."""
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE entity.validation_test_cases SET name=%s, jurisdiction_country=%s, "
                "jurisdiction_state=%s, registry_id=%s, expect_status=%s, note=%s, "
                "last_result=NULL, last_run_at=NULL WHERE id=%s",
                _case_fields(case) + (cid,))
        c.commit()


def delete_case(cid: int) -> None:
    with closing(_conn()) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM entity.validation_test_cases WHERE id=%s", (cid,))
        c.commit()


def run_validation_case(config: dict, cid: int) -> dict:
    """Run the LIVE production validator for a stored case and grade actual vs expected status."""
    from agent import EntityLookup
    case = get_case(int(cid))
    if not case:
        return {"error": f"unknown case #{cid}"}
    report = {"recommended_entity": {
        "legal_entity_name": case["name"], "registry_id": (case.get("registry_id") or None),
        "jurisdiction_country": case.get("jurisdiction_country"),
        "jurisdiction_state": case.get("jurisdiction_state")}}
    try:
        agent = EntityLookup(config, progress_callback=None)
        agent.validate_entity_in_registry(report)
        rv = report.get("registry_validation") or {}
        actual = rv.get("status") or "unvalidated"
    except Exception as e:  # noqa: BLE001
        rv = {"error": f"{type(e).__name__}: {e}"}
        actual = "error"
    passed = (actual == case["expect_status"])
    result = {"id": cid, "actual_status": actual, "expect_status": case["expect_status"],
              "passed": passed, "registry_validation": rv}
    try:
        with closing(_conn()) as c:
            with c.cursor() as cur:
                cur.execute("UPDATE entity.validation_test_cases SET last_result=%s, last_run_at=now() WHERE id=%s",
                            (json.dumps(result, default=str), cid))
            c.commit()
    except Exception:  # noqa: BLE001
        pass
    return result
