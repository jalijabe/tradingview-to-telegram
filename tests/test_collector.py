import base64
import json

import pytest

from observer import observer
from tests.conftest import CHAT_ID
from tests.fakes import furl_event, log_records
from tvtg import collector

KEY = b"collector-key"


def store(aws, message_id=55, **ts):
    item = {"pk": {"S": f"{CHAT_ID}:{message_id}"}}
    item.update({k: {"N": str(v)} for k, v in ts.items()})
    aws.ddb.put_item(TableName=aws.table, Item=item)


def signed_event(body: bytes, key: bytes = KEY, **kwargs):
    return furl_event(body, ip="198.51.100.20", headers={"X-Sig": observer.sign(key, body)}, **kwargs)


def test_reception_record(aws, capsys):
    store(aws, t1=1_000, t2=1_400, t7=1_500, t8=1_700)
    body = observer.build_report(int(CHAT_ID), 55, 1_650)
    assert collector.handler(signed_event(body))["statusCode"] == 200
    [rec] = log_records(capsys.readouterr().out, "received")
    assert rec["pk"] == f"{CHAT_ID}:55"
    assert rec["t9"] == 1_650 and rec["tg_to_client"] == 150 and rec["tv_to_client"] == 650
    names = [m["Name"] for m in rec["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
    assert names == ["tg_to_client", "tv_to_client"]


def test_observer_label_is_recorded(aws, capsys):
    store(aws, t7=1_500)
    collector.handler(signed_event(observer.build_report(int(CHAT_ID), 55, 1_600, "pi-home")))
    collector.handler(signed_event(observer.build_report(int(CHAT_ID), 55, 1_600)))
    labels = [r["observer"] for r in log_records(capsys.readouterr().out, "received")]
    assert labels == ["pi-home", "unknown"]


def test_missing_t1_still_reports_tg_to_client(aws, capsys):
    store(aws, t7=1_500, t8=1_700)
    collector.handler(signed_event(observer.build_report(int(CHAT_ID), 55, 1_600)))
    [rec] = log_records(capsys.readouterr().out, "received")
    assert rec["tg_to_client"] == 100 and "tv_to_client" not in rec


def test_observer_and_collector_agree_on_signatures():
    body = b'{"a":1}'
    assert observer.sign(KEY, body) == collector.sign(KEY, body)


@pytest.mark.parametrize(
    "event_factory",
    [
        lambda body: furl_event(body, headers={}),  # no signature
        lambda body: signed_event(body, key=b"wrong-key"),
        lambda body: furl_event(body, headers={"x-sig": observer.sign(KEY, body + b" ")}),  # body tampered
    ],
)
def test_bad_signature_rejected(aws, event_factory):
    body = observer.build_report(int(CHAT_ID), 55, 1)
    assert collector.handler(event_factory(body))["statusCode"] == 401


def test_header_name_is_case_insensitive(aws):
    store(aws, t7=1)
    body = observer.build_report(int(CHAT_ID), 55, 2)
    event = furl_event(body, headers={"x-sig": observer.sign(KEY, body)})
    assert collector.handler(event)["statusCode"] == 200


def test_unknown_message_returns_404(aws):
    assert collector.handler(signed_event(observer.build_report(int(CHAT_ID), 999, 1)))["statusCode"] == 404


@pytest.mark.parametrize("body", [b"not json", b'{"chat_id": 1}', b'{"chat_id": "x", "message_id": 1, "recv_ms": 1}'])
def test_malformed_report(aws, body):
    assert collector.handler(signed_event(body))["statusCode"] == 400


def test_base64_body(aws):
    store(aws, t7=1)
    body = observer.build_report(int(CHAT_ID), 55, 2)
    event = signed_event(body)
    event.update(isBase64Encoded=True, body=base64.b64encode(body).decode())
    assert collector.handler(event)["statusCode"] == 200


def test_invalid_base64(aws):
    event = signed_event(b"x")
    event.update(isBase64Encoded=True, body="%%%")
    assert collector.handler(event)["statusCode"] == 400


def test_report_shape():
    assert json.loads(observer.build_report("-100", "5", 7.9)) == {"chat_id": -100, "message_id": 5, "recv_ms": 7}
