import socket

import pytest

from tests.fakes import bad_request, server_error, too_many_requests
from tvtg.telegram import TelegramClient, TgResult


def test_send_message_success_and_payload(telegram):
    tg = TelegramClient("123:ABC", telegram.url)
    res = tg.send_message(-100, "<b>hi</b>", "HTML", disable_notification=True)
    assert res.ok and res.status == 200 and res.new_conn
    assert res.body["result"]["message_id"] == 101
    path, body = telegram.requests[0]
    assert path == "/bot123:ABC/sendMessage"
    assert body == {"chat_id": -100, "text": "<b>hi</b>", "parse_mode": "HTML", "disable_notification": True,
                    "link_preview_options": {"is_disabled": True}}


def test_optional_fields_omitted(telegram):
    TelegramClient("t", telegram.url).send_message("-1", "x")
    body = telegram.requests[0][1]
    assert "parse_mode" not in body and "disable_notification" not in body


def test_connection_is_reused(telegram):
    tg = TelegramClient("t", telegram.url)
    first = tg.send_message(1, "a")
    second = tg.send_message(1, "b")
    assert first.new_conn and not second.new_conn
    assert telegram.connections == 1


def test_stale_keepalive_connection_is_retried_once_on_a_fresh_one(telegram):
    tg = TelegramClient("t", telegram.url)
    telegram.drop_after_next = True
    assert tg.send_message(1, "a").ok
    res = tg.send_message(1, "b")  # the server silently closed the socket after "a"
    assert res.ok and res.new_conn
    assert telegram.connections == 2
    assert [b["text"] for b in telegram.sent()] == ["a", "b"]


def test_fresh_connection_failure_raises():
    with socket.socket() as s:  # grab a free port, then close it so nothing listens there
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    tg = TelegramClient("t", f"http://127.0.0.1:{port}", timeout_s=1)
    with pytest.raises(OSError):
        tg.send_message(1, "x")


def test_error_responses_are_returned_not_raised(telegram):
    telegram.queue(too_many_requests(9), bad_request(), server_error())
    tg = TelegramClient("t", telegram.url)
    r429, r400, r502 = (tg.send_message(1, "x") for _ in range(3))
    assert (r429.status, r429.retry_after, r429.ok) == (429, 9, False)
    assert (r400.status, r400.retry_after) == (400, None)
    assert r502.status == 502 and r502.body == {"ok": False, "description": "<html>Bad Gateway</html>"}


def test_getme_via_call(telegram):
    res = TelegramClient("t", telegram.url).call("getMe", {})
    assert res.ok and res.body["result"]["is_bot"]


def test_close_is_idempotent(telegram):
    tg = TelegramClient("t", telegram.url)
    tg.close()
    tg.send_message(1, "x")
    tg.close()
    tg.close()
    assert tg.send_message(1, "y").new_conn


@pytest.mark.parametrize("url", ["ftp://x", "not a url", "https://"])
def test_bad_base_url(url):
    with pytest.raises(ValueError):
        TelegramClient("t", url)


def test_https_base_url_parsing():
    tg = TelegramClient("t", "https://api.telegram.org/")
    assert tg._https and tg._host == "api.telegram.org" and tg._port is None and tg._prefix == ""


def test_retry_after_ignores_non_numbers():
    assert TgResult(429, {"parameters": {"retry_after": "soon"}}, False).retry_after is None
    assert TgResult(200, {"ok": False}, False).ok is False
