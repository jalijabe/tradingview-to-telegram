"""Ingest Lambda (Function URL): check the TradingView webhook, stamp t2/t3, enqueue, return fast.

TradingView cancels requests after 3 s and never retries, so this does the minimum work. Telegram is
never called from here.

Environment:
  QUEUE_URL           SQS queue for the sender
  TV_SECRET_PARAM     SSM parameter name holding the shared webhook secret
  ALLOWED_IPS_PARAM   optional SSM parameter (comma-separated IPs); overrides ALLOWED_IPS
  ALLOWED_IPS         optional comma-separated IPs; defaults to TradingView's published webhook IPs
  ALLOW_ANY_IP        "true" disables the IP check (dev stage only)
"""

import base64
import binascii
import hmac
import json
import os
from typing import Any

from tvtg import config
from tvtg.clock import now_ms
from tvtg.log import log

TRADINGVIEW_IPS = ("52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7")
MAX_BODY_BYTES = 10_000
IP_LIST_TTL_S = 300

_warm = False


def _keepalive(cold: bool) -> dict[str, Any]:
    """Pay the cold-start costs (SSM secret, SQS client + TLS connection) before an alert has to."""
    try:
        config.param("TV_SECRET_PARAM")
        config.client("sqs").get_queue_attributes(QueueUrl=os.environ["QUEUE_URL"], AttributeNames=["QueueArn"])
    except Exception as exc:  # noqa: BLE001
        log("keepalive", fn="ingest", ingest_cold=cold, error=repr(exc))
        return {"keepalive": False}
    log("keepalive", fn="ingest", ingest_cold=cold)
    return {"keepalive": True}


def _response(status: int, body: str = "") -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "text/plain"}, "body": body}


def _allowed_ips() -> set[str]:
    if os.environ.get("ALLOWED_IPS_PARAM"):
        raw = config.param("ALLOWED_IPS_PARAM", ttl_s=IP_LIST_TTL_S)
    else:
        raw = os.environ.get("ALLOWED_IPS") or ",".join(TRADINGVIEW_IPS)
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


def _ip_ok(ip: str) -> bool:
    if os.environ.get("ALLOW_ANY_IP", "").lower() == "true":
        return True
    return ip in _allowed_ips()


def _body(event: dict[str, Any]) -> str | None:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    return body


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    global _warm
    t3 = now_ms()
    cold, _warm = not _warm, True

    if event.get("keepalive"):  # scheduled direct invoke; a Function URL request can't produce this shape
        return _keepalive(cold)

    rc = event.get("requestContext") or {}
    http = rc.get("http") or {}
    ip = http.get("sourceIp", "")
    request_id = rc.get("requestId")

    def reject(status: int, reason: str) -> dict[str, Any]:
        log("rejected", reason=reason, status=status, source_ip=ip, request_id=request_id)
        return _response(status)

    if http.get("method", "POST").upper() != "POST":
        return reject(405, "method")
    if not _ip_ok(ip):
        return reject(403, "ip")

    body = _body(event)
    if body is None:
        return reject(400, "encoding")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        return reject(413, "size")
    try:
        alert = json.loads(body)
    except ValueError:
        return reject(400, "json")
    if not isinstance(alert, dict):
        return reject(400, "json")

    secret = alert.pop("secret", None)  # removed before anything is logged or queued
    expected = config.param("TV_SECRET_PARAM")
    if not isinstance(secret, str) or not hmac.compare_digest(secret.encode(), expected.encode()):
        return reject(401, "secret")

    alert["_t"] = {"id": request_id, "t2": rc.get("timeEpoch"), "t3": t3, "ingest_cold": cold}
    # Recovery copy: if the SQS send below fails, the alert can be replayed from this log line.
    log("ingest", alert_id=request_id, alert=alert)

    try:
        config.client("sqs").send_message(QueueUrl=os.environ["QUEUE_URL"], MessageBody=json.dumps(alert))
    except Exception as exc:  # noqa: BLE001 - any failure means "not queued"
        log("ingest_failed", alert_id=request_id, error=repr(exc))
        return _response(500)
    return _response(200, "ok")
