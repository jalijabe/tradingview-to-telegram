"""Whole pipeline, locally: TradingView webhook -> ingest -> SQS -> sender -> Telegram -> observer -> collector.

Real handler code runs against moto (SSM, SQS, DynamoDB) and a real HTTP fake of the Telegram Bot API.
The graph tooling is then run over the records the pipeline actually logged.
"""

from datetime import UTC, datetime

from observer import observer
from tests.conftest import CHAT_ID
from tests.fakes import alert, furl_event, log_records, sqs_records
from tools import graphs
from tvtg import collector, ingest, sender
from tvtg.clock import now_ms
from tvtg.timing import PATH_SEGMENTS


def test_full_pipeline(aws, capsys, tmp_path):
    n = 3
    for i in range(n):
        fired_at = datetime.fromtimestamp((now_ms() - 400) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        tv_alert = alert(fired_at=fired_at, msg=f"signal {i}")
        assert ingest.handler(furl_event(tv_alert, request_id=f"req-{i}"))["statusCode"] == 200

        records = sqs_records(aws.sqs, aws.queue_url)
        assert len(records) == 1
        assert sender.handler({"Records": records}) == {"batchItemFailures": []}
        aws.sqs.delete_message(QueueUrl=aws.queue_url, ReceiptHandle=records[0]["receiptHandle"])

        # The observer sees the channel post and reports it.
        message_id = aws.telegram.responses[-1][1]["result"]["message_id"]
        body = observer.build_report(int(CHAT_ID), message_id, now_ms())
        event = furl_event(body, ip="198.51.100.20", headers={"x-sig": observer.sign(b"collector-key", body)})
        assert collector.handler(event)["statusCode"] == 200

    out = capsys.readouterr().out
    delivered = log_records(out, "delivered")
    received = log_records(out, "received")
    assert len(delivered) == len(received) == n
    assert [m["text"].splitlines()[1] for m in aws.telegram.sent()] == [f"signal {i}" for i in range(n)]

    first, *rest = delivered
    assert first["ingest_cold"] and first["sender_cold"] and first["tg_new_conn"]
    assert not any(r["ingest_cold"] or r["sender_cold"] or r["tg_new_conn"] for r in rest)

    for d in delivered:
        hops = [name for name, _, _ in PATH_SEGMENTS]
        assert all(name in d for name in hops)
        assert all(d[name] >= 0 for name in hops if name != "fire_to_edge")
        assert sum(d[h] for h in hops) == d["end_to_end"]
    for r in received:
        assert r["tg_to_client"] >= 0 and r["tv_to_client"] >= r["tg_to_client"]

    # The same graph tooling used for mock data works on what the pipeline logged.
    export = tmp_path / "export.jsonl"
    export.write_text(out)
    outputs = graphs.render_all([export], tmp_path / "graphs")
    assert {p.name for p in outputs} >= {"waterfall.png", "summary.md"}
    assert f"{n} alerts" in (tmp_path / "graphs" / "summary.md").read_text()
