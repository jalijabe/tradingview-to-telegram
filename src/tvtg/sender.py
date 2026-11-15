"""Sender Lambda (SQS trigger): render, send to Telegram, emit the per-alert timing record.

Once Telegram has accepted a message, everything after that is best-effort. A logging or DynamoDB failure
must never make SQS redeliver the message, because that would post a duplicate alert.

Environment:
  BOT_TOKEN_PARAM     SSM parameter name holding the bot token
  CHAT_ID             alert channel id (e.g. -100123...)
  CANARY_CHAT_ID      optional muted channel for {"canary": true} alerts
  QUEUE_URL           the source queue (used to delay retries after a 429)
  TIMING_TABLE        optional DynamoDB table for reception matching (Phase 4)
  TELEGRAM_BASE_URL   default https://api.telegram.org (tests point this at a fake server)

A scheduled ``{"keepalive": true}`` event (optional, see infra var keepalive_minutes) calls getMe so the
container and its Telegram connection stay warm.
"""

import json
import os
from typing import Any

from tvtg import config, emf, timing
from tvtg.clock import now_ms
from tvtg.log import emit, log
from tvtg.render import render_html, render_plain
from tvtg.telegram import TelegramClient, TgResult

TIMING_TTL_S = 86_400
MAX_VISIBILITY_S = 43_200

_warm = False
_tg: TelegramClient | None = None


def _telegram() -> TelegramClient:
    global _tg
    if _tg is None:
        _tg = TelegramClient(
            config.param("BOT_TOKEN_PARAM"),
            os.environ.get("TELEGRAM_BASE_URL", "https://api.telegram.org"),
        )
    return _tg


def reset() -> None:
    """Simulate a fresh container (tests)."""
    global _warm, _tg
    if _tg is not None:
        _tg.close()
    _warm, _tg = False, None


def _chat_for(alert: dict[str, Any]) -> tuple[str, bool]:
    if alert.get("canary") is True and os.environ.get("CANARY_CHAT_ID"):
        return os.environ["CANARY_CHAT_ID"], True
    return os.environ["CHAT_ID"], False


def _delay_retry(receipt_handle: str, seconds: int) -> None:
    try:
        config.client("sqs").change_message_visibility(
            QueueUrl=os.environ["QUEUE_URL"],
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=max(1, min(seconds, MAX_VISIBILITY_S)),
        )
    except Exception as exc:  # noqa: BLE001 - fall back to the queue's default visibility timeout
        log("visibility_failed", error=repr(exc))


def _store_timing(chat_id: str, message_id: int, ts: dict[str, int | None]) -> None:
    table = os.environ.get("TIMING_TABLE")
    if not table:
        return
    item: dict[str, Any] = {"pk": {"S": f"{chat_id}:{message_id}"}}
    for key in ("t1", "t2", "t7", "t8"):
        if ts.get(key) is not None:
            item[key] = {"N": str(ts[key])}
    item["ttl"] = {"N": str(int(ts["t8"] or 0) // 1000 + TIMING_TTL_S)}
    config.client("dynamodb").put_item(TableName=table, Item=item)


def _process(rec: dict[str, Any], sender_cold: bool) -> bool:
    """Handle one SQS record. Returns True to acknowledge it, False to have SQS retry it."""
    t6 = now_ms()
    try:
        alert = json.loads(rec["body"])
        if not isinstance(alert, dict):
            raise ValueError("body is not an object")
    except (ValueError, KeyError) as exc:
        log("dropped", reason="bad_body", message_id=rec.get("messageId"), error=repr(exc))
        return True  # a poison message would never succeed; don't retry it

    meta = alert.pop("_t", None) or {}
    attrs = rec.get("attributes") or {}
    alert_id = meta.get("id") or rec.get("messageId")
    chat_id, is_canary = _chat_for(alert)

    try:
        tg = _telegram()
        t7 = now_ms()
        res: TgResult = tg.send_message(chat_id, render_html(alert), "HTML", disable_notification=is_canary)
        fallback = False
        if res.status == 400:
            # Usually a formatting problem; plain text has no markup to reject.
            t7 = now_ms()
            res = tg.send_message(chat_id, render_plain(alert), None, disable_notification=is_canary)
            fallback = True
        t8 = now_ms()
    except Exception as exc:  # noqa: BLE001 - network failure -> let SQS retry
        log("send_failed", alert_id=alert_id, error=repr(exc))
        return False

    if res.status == 429:
        log("rate_limited", alert_id=alert_id, retry_after=res.retry_after)
        if res.retry_after and rec.get("receiptHandle"):
            _delay_retry(rec["receiptHandle"], res.retry_after + 1)
        return False
    if res.status >= 500:
        log("send_failed", alert_id=alert_id, telegram_status=res.status)
        return False
    if not res.ok:
        log("dropped", reason="telegram_rejected", alert_id=alert_id, telegram_status=res.status,
            description=res.body.get("description"))
        return True

    # Telegram accepted the message. Everything below is best-effort.
    try:
        message_id = (res.body.get("result") or {}).get("message_id")
        ts: dict[str, int | None] = {
            "t0": timing.bar_close_ms(alert.get("bar_time"), alert.get("interval")),
            "t1": timing.iso_to_ms(alert.get("fired_at")),
            "t2": meta.get("t2"),
            "t3": meta.get("t3"),
            "t4": int(attrs["SentTimestamp"]) if "SentTimestamp" in attrs else None,
            "t5": int(attrs["ApproximateFirstReceiveTimestamp"])
            if "ApproximateFirstReceiveTimestamp" in attrs
            else None,
            "t6": t6,
            "t7": t7,
            "t8": t8,
        }
        segs = timing.segments(ts, timing.DELIVERY_SEGMENTS)
        props = {
            "event": "delivered",
            "alert_id": alert_id,
            "ticker": alert.get("ticker"),
            "interval": alert.get("interval"),
            "canary": is_canary,
            "chat_id": chat_id,
            "message_id": message_id,
            **ts,
            **segs,
            "ingest_cold": bool(meta.get("ingest_cold")),
            "sender_cold": sender_cold,
            "tg_new_conn": res.new_conn,
            "receive_count": int(attrs.get("ApproximateReceiveCount", 1)),
            "telegram_status": res.status,
            "fallback": fallback,
        }
        metrics = {"end_to_end": segs.get("end_to_end"), "aws_total": segs.get("aws_total")}
        emit(emf.record(metrics, props, t8))
        if message_id is not None:
            _store_timing(chat_id, message_id, ts)
    except Exception as exc:  # noqa: BLE001 - never redeliver an already-sent alert
        log("post_send_failed", alert_id=alert_id, error=repr(exc))
    return True


def _keepalive(cold: bool) -> dict[str, Any]:
    """Scheduled ping: keeps this container and its Telegram connection warm between sparse alerts."""
    t0 = now_ms()
    try:
        res = _telegram().call("getMe", {})
    except Exception as exc:  # noqa: BLE001 - a failed ping only costs the next alert a handshake
        log("keepalive", fn="sender", sender_cold=cold, error=repr(exc))
        return {"keepalive": False}
    log("keepalive", fn="sender", sender_cold=cold, tg_new_conn=res.new_conn, telegram_status=res.status,
        rtt_ms=now_ms() - t0)
    return {"keepalive": res.ok}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    global _warm
    cold, _warm = not _warm, True
    if event.get("keepalive"):
        return _keepalive(cold)
    failures = []
    for rec in event.get("Records", []):
        try:
            ok = _process(rec, cold)
        except Exception as exc:  # noqa: BLE001 - one bad record must not fail the batch
            log("sender_error", message_id=rec.get("messageId"), error=repr(exc))
            ok = False
        if not ok:
            failures.append({"itemIdentifier": rec.get("messageId")})
    return {"batchItemFailures": failures}
