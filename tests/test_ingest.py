import json

import pytest

from tests.fakes import TV_IP, alert, furl_event, log_records, sqs_records
from tvtg import config, ingest


def _queued(aws):
    return [json.loads(r["body"]) for r in sqs_records(aws.sqs, aws.queue_url)]


def test_valid_alert_is_queued_without_secret(aws, capsys):
    event = furl_event(alert(), request_id="abc", time_epoch=1_000)
    resp = ingest.handler(event)
    assert resp["statusCode"] == 200 and resp["body"] == "ok"

    [msg] = _queued(aws)
    assert "secret" not in msg
    assert msg["ticker"] == "BTCUSD"
    meta = msg["_t"]
    assert meta["id"] == "abc" and meta["t2"] == 1_000 and meta["ingest_cold"] is True
    assert meta["t3"] > meta["t2"]

    out = capsys.readouterr().out
    assert "tv-secret" not in out  # the secret never reaches the logs
    [rec] = log_records(out, "ingest")
    assert rec["alert"]["_t"]["id"] == "abc"


def test_cold_flag_only_on_first_invocation(aws):
    ingest.handler(furl_event(alert()))
    ingest.handler(furl_event(alert()))
    assert [m["_t"]["ingest_cold"] for m in _queued(aws)] == [True, False]


@pytest.mark.parametrize(
    ("event_kwargs", "body", "status", "reason"),
    [
        ({"ip": "1.2.3.4"}, alert(), 403, "ip"),
        ({"method": "GET"}, alert(), 405, "method"),
        ({}, alert(secret="wrong"), 401, "secret"),  # pragma: allowlist secret
        ({}, {k: v for k, v in alert().items() if k != "secret"}, 401, "secret"),
        ({}, alert(secret=12345), 401, "secret"),
        ({}, "not json", 400, "json"),
        ({}, "[1, 2]", 400, "json"),
        ({}, {"secret": "tv-secret", "msg": "x" * 20_000}, 413, "size"),  # pragma: allowlist secret
    ],
)
def test_rejections(aws, capsys, event_kwargs, body, status, reason):
    resp = ingest.handler(furl_event(body, **event_kwargs))
    assert resp["statusCode"] == status
    [rec] = log_records(capsys.readouterr().out, "rejected")
    assert rec["reason"] == reason and rec["status"] == status
    assert _queued(aws) == []


def test_rejected_secret_is_not_logged(aws, capsys):
    ingest.handler(furl_event(alert(secret="guess-123")))  # pragma: allowlist secret
    assert "guess-123" not in capsys.readouterr().out


def test_base64_body(aws):
    assert ingest.handler(furl_event(alert(), b64=True))["statusCode"] == 200
    assert len(_queued(aws)) == 1


def test_invalid_base64_body(aws):
    event = furl_event(alert())
    event.update(isBase64Encoded=True, body="!!!not base64!!!")
    assert ingest.handler(event)["statusCode"] == 400


def test_allow_any_ip_for_dev(aws, monkeypatch):
    monkeypatch.setenv("ALLOW_ANY_IP", "true")
    assert ingest.handler(furl_event(alert(), ip="203.0.113.9"))["statusCode"] == 200


def test_allowed_ips_env_override(aws, monkeypatch):
    monkeypatch.setenv("ALLOWED_IPS", "203.0.113.9, 198.51.100.1")
    assert ingest.handler(furl_event(alert(), ip="198.51.100.1"))["statusCode"] == 200
    assert ingest.handler(furl_event(alert(), ip=TV_IP))["statusCode"] == 403


def test_allowed_ips_from_ssm_is_cached_with_ttl(aws, monkeypatch):
    aws.ssm.put_parameter(Name="/tvtg/test/allowed_ips", Value="203.0.113.9", Type="StringList")
    monkeypatch.setenv("ALLOWED_IPS_PARAM", "/tvtg/test/allowed_ips")
    assert ingest.handler(furl_event(alert(), ip="203.0.113.9"))["statusCode"] == 200

    aws.ssm.put_parameter(Name="/tvtg/test/allowed_ips", Value="198.51.100.7", Type="StringList", Overwrite=True)
    assert ingest.handler(furl_event(alert(), ip="198.51.100.7"))["statusCode"] == 403  # still cached

    clock = [10_000.0]
    monkeypatch.setattr(config.time, "monotonic", lambda: clock[0])
    config._params.clear()
    ingest.handler(furl_event(alert(), ip="198.51.100.7"))  # re-read at t=10000
    aws.ssm.put_parameter(Name="/tvtg/test/allowed_ips", Value="192.0.2.1", Type="StringList", Overwrite=True)
    clock[0] += ingest.IP_LIST_TTL_S + 1
    assert ingest.handler(furl_event(alert(), ip="192.0.2.1"))["statusCode"] == 200  # TTL expired


def test_sqs_failure_returns_500_and_keeps_recovery_copy(aws, monkeypatch, capsys):
    monkeypatch.setenv("QUEUE_URL", aws.queue_url + "-missing")
    resp = ingest.handler(furl_event(alert(), request_id="lost-1"))
    assert resp["statusCode"] == 500
    out = capsys.readouterr().out
    assert log_records(out, "ingest")[0]["alert_id"] == "lost-1"
    assert log_records(out, "ingest_failed")[0]["alert_id"] == "lost-1"


def test_keepalive_warms_container(aws, capsys):
    assert ingest.handler({"keepalive": True}) == {"keepalive": True}
    [rec] = log_records(capsys.readouterr().out, "keepalive")
    assert rec["ingest_cold"] is True and rec["fn"] == "ingest"
    ingest.handler(furl_event(alert()))
    assert _queued(aws)[0]["_t"]["ingest_cold"] is False  # the alert found a warm container


def test_keepalive_failure_is_reported(aws, monkeypatch, capsys):
    monkeypatch.setenv("QUEUE_URL", aws.queue_url + "-missing")
    assert ingest.handler({"keepalive": True}) == {"keepalive": False}
    assert "error" in log_records(capsys.readouterr().out, "keepalive")[0]
