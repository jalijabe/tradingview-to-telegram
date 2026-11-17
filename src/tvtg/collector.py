"""Collector Lambda (Function URL): receive observer reports and emit reception timings.

The observer (your own Telegram account) reports ``{chat_id, message_id, recv_ms, observer?}``, signed with
HMAC-SHA256 in the ``X-Sig`` header. The sender stored t1/t2/t7/t8 under ``chat_id:message_id``.

Environment:
  COLLECTOR_KEY_PARAM   SSM parameter name holding the shared HMAC key
  TIMING_TABLE          DynamoDB table written by the sender
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
from typing import Any

from tvtg import config, emf, timing
from tvtg.log import emit, log


def _response(status: int) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "text/plain"}, "body": ""}


def sign(key: bytes, body: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(raw, validate=True)
        except binascii.Error:
            return _response(400)
    else:
        body = raw.encode("utf-8")

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    key = config.param("COLLECTOR_KEY_PARAM").encode()
    if not hmac.compare_digest(headers.get("x-sig", ""), sign(key, body)):
        log("rejected", reason="signature")
        return _response(401)

    try:
        report = json.loads(body)
        pk = f"{int(report['chat_id'])}:{int(report['message_id'])}"
        t9 = int(report["recv_ms"])
        observer = str(report.get("observer") or "unknown")[:64]
    except (ValueError, KeyError, TypeError, AttributeError):
        return _response(400)

    item = (
        config.client("dynamodb")
        .get_item(TableName=os.environ["TIMING_TABLE"], Key={"pk": {"S": pk}})
        .get("Item")
    )
    if not item:
        return _response(404)  # the sender hasn't stored it yet; the observer retries

    ts: dict[str, int | None] = {k: int(item[k]["N"]) if k in item else None for k in ("t1", "t2", "t7", "t8")}
    ts["t9"] = t9
    segs = timing.segments(ts, timing.RECEPTION_SEGMENTS)
    props = {"event": "received", "pk": pk, "observer": observer, **ts, **segs}
    emit(emf.record({"tg_to_client": segs.get("tg_to_client"), "tv_to_client": segs.get("tv_to_client")},
                    props, t9))
    return _response(200)
