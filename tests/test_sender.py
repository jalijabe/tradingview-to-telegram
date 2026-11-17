import json

import pytest

from tests.conftest import CANARY_CHAT_ID, CHAT_ID
from tests.fakes import bad_request, log_records, server_error, sqs_record, too_many_requests
from tvtg import sender, timing


def queued_alert(**overrides):
    body = {"ticker": "BTCUSD", "exchange": "COINBASE", "interval": "5", "price": "97000",
            "bar_time": "2026-11-14T14:00:00Z", "fired_at": "2026-11-14T14:05:00Z", "msg": "Long",
            "canary": False, "_t": {"id": "req-9", "t2": 1_794_665_100_400, "t3": 1_794_665_100_410,
                                    "ingest_cold": True}}
    body.update(overrides)
    return body


def run(*records):
    return sender.handler({"Records": list(records)})


def test_delivers_and_emits_timing_record(aws, capsys):
    rec = sqs_record(queued_alert(), SentTimestamp=1_794_665_100_430,
                     ApproximateFirstReceiveTimestamp=1_794_665_100_460)
    assert run(rec) == {"batchItemFailures": []}

    [sent] = aws.telegram.sent()
    assert sent["chat_id"] == CHAT_ID and sent["parse_mode"] == "HTML" and "disable_notification" not in sent
    assert "<b>BTCUSD</b>" in sent["text"]

    [d] = log_records(capsys.readouterr().out, "delivered")
    assert d["alert_id"] == "req-9" and d["message_id"] == 101 and d["chat_id"] == CHAT_ID
    assert d["t0"] == timing.iso_to_ms("2026-11-14T14:05:00Z")  # bar open + 5 min
    assert d["t1"] == timing.iso_to_ms("2026-11-14T14:05:00Z")
    assert (d["t2"], d["t3"], d["t4"], d["t5"]) == (1_794_665_100_400, 1_794_665_100_410, 1_794_665_100_430,
                                                    1_794_665_100_460)
    assert d["t6"] <= d["t7"] <= d["t8"]
    assert d["fire_to_edge"] == 400 and d["edge_to_ingest"] == 10 and d["ingest_to_queue"] == 20
    assert d["queue_wait"] == 30 and d["bar_to_fire"] == 0
    assert d["end_to_end"] == d["t8"] - d["t1"] and d["aws_total"] == d["t8"] - d["t2"]
    assert d["ingest_cold"] is True and d["sender_cold"] is True and d["tg_new_conn"] is True
    assert d["receive_count"] == 1 and d["telegram_status"] == 200 and d["fallback"] is False
    names = [m["Name"] for m in d["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
    assert names == ["end_to_end", "aws_total"]

    item = aws.ddb.get_item(TableName=aws.table, Key={"pk": {"S": f"{CHAT_ID}:101"}})["Item"]
    assert {k: int(v["N"]) for k, v in item.items() if k != "pk"} == {
        "t1": d["t1"], "t2": d["t2"], "t7": d["t7"], "t8": d["t8"], "ttl": d["t8"] // 1000 + 86_400}


def test_second_invocation_is_warm_and_reuses_connection(aws, capsys):
    run(sqs_record(queued_alert()))
    run(sqs_record(queued_alert(), message_id="m-2"))
    first, second = log_records(capsys.readouterr().out, "delivered")
    assert (first["sender_cold"], first["tg_new_conn"]) == (True, True)
    assert (second["sender_cold"], second["tg_new_conn"]) == (False, False)
    assert aws.telegram.connections == 1


def test_canary_goes_to_muted_canary_channel(aws, monkeypatch, capsys):
    monkeypatch.setenv("CANARY_CHAT_ID", CANARY_CHAT_ID)
    run(sqs_record(queued_alert(canary=True)))
    [sent] = aws.telegram.sent()
    assert sent["chat_id"] == CANARY_CHAT_ID and sent["disable_notification"] is True
    assert sent["text"].startswith("[canary]")
    assert log_records(capsys.readouterr().out, "delivered")[0]["canary"] is True


def test_canary_without_canary_channel_uses_main_chat(aws):
    run(sqs_record(queued_alert(canary=True)))
    assert aws.telegram.sent()[0]["chat_id"] == CHAT_ID


def test_429_fails_the_record_and_delays_retry(aws, capsys):
    aws.sqs.send_message(QueueUrl=aws.queue_url, MessageBody=json.dumps(queued_alert()))
    msg = aws.sqs.receive_message(QueueUrl=aws.queue_url, VisibilityTimeout=0)["Messages"][0]
    aws.telegram.queue(too_many_requests(30))
    rec = sqs_record(queued_alert(), message_id=msg["MessageId"])
    rec["receiptHandle"] = msg["ReceiptHandle"]

    assert run(rec) == {"batchItemFailures": [{"itemIdentifier": msg["MessageId"]}]}
    assert log_records(capsys.readouterr().out, "rate_limited")[0]["retry_after"] == 30
    # Visibility was extended to retry_after + 1, so the message isn't immediately receivable again.
    assert "Messages" not in aws.sqs.receive_message(QueueUrl=aws.queue_url)


def test_429_visibility_failure_is_logged_not_raised(aws, monkeypatch, capsys):
    monkeypatch.setenv("QUEUE_URL", aws.queue_url + "-missing")
    aws.telegram.queue(too_many_requests(5))
    assert run(sqs_record(queued_alert()))["batchItemFailures"]
    assert log_records(capsys.readouterr().out, "visibility_failed")


def test_5xx_fails_the_record(aws, capsys):
    aws.telegram.queue(server_error())
    assert run(sqs_record(queued_alert(), message_id="m-5"))["batchItemFailures"] == [{"itemIdentifier": "m-5"}]
    assert log_records(capsys.readouterr().out, "send_failed")[0]["telegram_status"] == 502


def test_400_falls_back_to_plain_text(aws, capsys):
    aws.telegram.queue(bad_request())
    assert run(sqs_record(queued_alert(msg="a < b")))["batchItemFailures"] == []
    html, plain = aws.telegram.sent()
    assert html["parse_mode"] == "HTML" and "parse_mode" not in plain
    assert "a < b" in plain["text"]
    assert log_records(capsys.readouterr().out, "delivered")[0]["fallback"] is True


def test_permanent_rejection_is_dropped_not_retried(aws, capsys):
    aws.telegram.queue(bad_request(), bad_request("Bad Request: chat not found"))
    assert run(sqs_record(queued_alert()))["batchItemFailures"] == []
    [rec] = log_records(capsys.readouterr().out, "dropped")
    assert rec["reason"] == "telegram_rejected" and "chat not found" in rec["description"]


def test_403_is_dropped(aws, capsys):
    aws.telegram.queue((403, {"ok": False, "description": "Forbidden: bot was kicked"}))
    assert run(sqs_record(queued_alert()))["batchItemFailures"] == []
    assert log_records(capsys.readouterr().out, "dropped")


def test_network_failure_fails_the_record(aws, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BASE_URL", "http://127.0.0.1:9")  # discard port: connection refused
    assert run(sqs_record(queued_alert(), message_id="m-n"))["batchItemFailures"] == [{"itemIdentifier": "m-n"}]
    assert log_records(capsys.readouterr().out, "send_failed")


@pytest.mark.parametrize("body", ["not json", "[1]", "null"])
def test_poison_message_is_dropped(aws, capsys, body):
    assert run(sqs_record(body))["batchItemFailures"] == []
    assert log_records(capsys.readouterr().out, "dropped")[0]["reason"] == "bad_body"
    assert aws.telegram.sent() == []


def test_post_send_failure_never_causes_redelivery(aws, monkeypatch, capsys):
    monkeypatch.setenv("TIMING_TABLE", "no-such-table")
    assert run(sqs_record(queued_alert()))["batchItemFailures"] == []
    out = capsys.readouterr().out
    assert log_records(out, "delivered") and log_records(out, "post_send_failed")


def test_no_timing_table_skips_dynamodb(aws, monkeypatch):
    monkeypatch.delenv("TIMING_TABLE")
    assert run(sqs_record(queued_alert()))["batchItemFailures"] == []
    assert aws.ddb.scan(TableName=aws.table)["Count"] == 0


def test_partial_batch_failure(aws):
    aws.telegram.queue((200, {"ok": True, "result": {"message_id": 7}}), server_error())
    result = run(sqs_record(queued_alert(), message_id="ok-1"), sqs_record(queued_alert(), message_id="bad-2"))
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-2"}]}


def test_missing_metadata_still_delivers(aws, capsys):
    body = {"ticker": "X", "fired_at": "garbage"}
    rec = {"messageId": "m", "receiptHandle": "r", "body": json.dumps(body), "attributes": {}}
    assert run(rec)["batchItemFailures"] == []
    [d] = log_records(capsys.readouterr().out, "delivered")
    assert d["t1"] is None and d["t2"] is None and "end_to_end" not in d
    assert d["telegram_rtt"] >= 0 and d["alert_id"] == "m"
    assert "_aws" not in d  # no metric values -> no metrics declared


def test_retried_message_reports_receive_count(aws, capsys):
    run(sqs_record(queued_alert(), ApproximateReceiveCount=3))
    assert log_records(capsys.readouterr().out, "delivered")[0]["receive_count"] == 3


def test_unexpected_exception_fails_only_that_record(aws, monkeypatch, capsys):
    def boom(rec, cold):
        raise RuntimeError("bug")

    monkeypatch.setattr(sender, "_process", boom)
    assert run(sqs_record(queued_alert(), message_id="x"))["batchItemFailures"] == [{"itemIdentifier": "x"}]
    assert log_records(capsys.readouterr().out, "sender_error")


def test_keepalive_opens_and_then_reuses_the_connection(aws, capsys):
    assert sender.handler({"keepalive": True}) == {"keepalive": True}
    run(sqs_record(queued_alert()))
    out = capsys.readouterr().out
    [ka] = log_records(out, "keepalive")
    assert ka["sender_cold"] is True and ka["tg_new_conn"] is True and ka["telegram_status"] == 200
    [d] = log_records(out, "delivered")
    assert d["sender_cold"] is False and d["tg_new_conn"] is False
    assert aws.telegram.requests[0][0].endswith("/getMe")


def test_keepalive_failure(aws, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BASE_URL", "http://127.0.0.1:9")
    assert sender.handler({"keepalive": True}) == {"keepalive": False}
    assert "error" in log_records(capsys.readouterr().out, "keepalive")[0]
