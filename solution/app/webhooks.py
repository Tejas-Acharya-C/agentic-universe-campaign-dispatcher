"""FastAPI webhook endpoint for telephony call result webhooks."""

import json
from fastapi import APIRouter, HTTPException, Request

from solution.app.rules import verify_hmac_signature
from solution.app.state_machine import apply_call_result

router = APIRouter()


@router.post("/webhooks/telephony")
async def telephony_webhook(request: Request):
    """
    Receive, authenticate, and process call result webhooks from telephony provider.
    """
    db_conn = getattr(request.app.state, "db", None)
    if db_conn is None:
        raise HTTPException(status_code=500, detail="Database connection not available")

    body_bytes = await request.body()
    if not body_bytes:
        raise HTTPException(status_code=400, detail="Empty request body")

    # Fetch configured webhook secret from system_metadata
    secret_row = db_conn.execute(
        "SELECT value FROM system_metadata WHERE key = 'webhook_secret';"
    ).fetchone()
    webhook_secret = secret_row["value"] if secret_row else ""

    signature = request.headers.get("X-AU-Signature")
    if not verify_hmac_signature(webhook_secret, body_bytes, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        raw_body_text = body_bytes.decode("utf-8")
        payload = json.loads(raw_body_text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Malformed JSON: {exc}")

    # Process state transition atomically inside write transaction
    apply_call_result(db_conn, payload, raw_webhook_body=raw_body_text)

    return {"status": "ok"}

