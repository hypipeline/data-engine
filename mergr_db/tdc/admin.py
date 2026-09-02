"""
TDC — the only code that writes upstream to DynamoDB from Data Engine.

Reads live in tdc.service (Postgres) and the pull lives in tdc.sync. Upstream
writes are kept here, alone, so the surface an admin can mutate stays small
enough to read in one sitting.

Exactly one such write exists: lifting a sandbox.

Consent is not writable from here, and not by policy alone — the instance role
carries a dynamodb:Attributes condition naming the delivery-axis attributes, so
an UpdateItem touching `status` is refused by IAM before it reaches the table.
Unsubscribing stays the subscriber's act, through the public Lambda.
"""
import os
from datetime import datetime, timezone

TABLE = os.environ.get("TDC_SUBSCRIBERS_TABLE", "dealchronicle-subscribers")
REGION = os.environ.get("TDC_DDB_REGION", "us-east-1")


class NotLiftable(Exception):
    """The address is not in a state a sandbox can be lifted from."""


def _ddb():
    # Imported lazily for the same reason as tdc.sync: a missing boto3 should
    # break this button, not the dashboard that imports the module at startup.
    import boto3
    return boto3.client("dynamodb", region_name=REGION)


def _why_not(ddb, email):
    """Turn a failed conditional write into something an admin can act on."""
    got = ddb.get_item(TableName=TABLE, Key={"pk": {"S": f"sub#{email}"}})
    item = got.get("Item")
    if not item:
        return "no such subscriber upstream"
    delivery = (item.get("delivery") or {}).get("S") or "ok"
    if delivery == "ok":
        return "not sandboxed"
    if delivery == "complained":
        return ("marked as a spam complaint, which an admin does not reverse — "
                "they asked their provider to stop this mail")
    return f"delivery is '{delivery}'"


def unsandbox(conn, email, actor):
    """Lift a sandbox: DynamoDB first, then the replica.

    Upstream is written first and conditionally. If the write is refused the
    replica is left alone, so the two can never disagree about who is mailable.

    Only `sandboxed` lifts. A spam complaint is deliberately not liftable here —
    the record is kept, and re-mailing someone who pressed the spam button is how
    a sending domain is lost.
    """
    email = (email or "").strip().lower()
    if not email:
        raise NotLiftable("no address given")

    from botocore.exceptions import ClientError
    ddb = _ddb()
    now = datetime.now(timezone.utc)

    # Captured before the write so the audit keeps what the row is about to lose.
    with conn.cursor() as cur:
        cur.execute("SELECT bounce_type, bounce_reason FROM tdc.subscribers WHERE email=%s",
                    (email,))
        prior = cur.fetchone() or (None, None)

    try:
        ddb.update_item(
            TableName=TABLE,
            Key={"pk": {"S": f"sub#{email}"}},
            UpdateExpression=("SET #delivery = :ok, #at = :now, #by = :actor "
                              "REMOVE #btype, #breason, #sboxed"),
            ConditionExpression="#delivery = :sandboxed",
            ExpressionAttributeNames={
                "#delivery": "delivery", "#at": "unsandboxedAt", "#by": "unsandboxedBy",
                "#btype": "bounceType", "#breason": "bounceReason", "#sboxed": "sandboxedAt",
            },
            ExpressionAttributeValues={
                ":ok": {"S": "ok"},
                ":now": {"S": now.isoformat()},
                ":actor": {"S": actor or "unknown"},
                ":sandboxed": {"S": "sandboxed"},
            },
            ReturnValues="NONE",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise NotLiftable(_why_not(ddb, email))
        raise

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE tdc.subscribers
               SET delivery='ok', bounce_type=NULL, bounce_reason=NULL, sandboxed_at=NULL,
                   unsandboxed_at=%s, unsandboxed_by=%s, synced_at=now()
             WHERE email=%s
        """, (now, actor, email))
    conn.commit()

    return {"email": email, "prior_bounce_type": prior[0], "prior_bounce_reason": prior[1]}
