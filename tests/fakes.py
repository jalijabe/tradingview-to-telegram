"""Test doubles: a local fake Telegram Bot API server and event builders."""

import base64
import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tvtg.clock import now_ms

TV_IP = "52.89.214.238"

Response = tuple[int, dict[str, Any] | bytes]


class FakeTelegram:
    """Real HTTP/1.1 keep-alive server, so the client's connection handling is exercised for real."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[Response] = []
        self.queued: list[Response | Callable[[str, dict[str, Any]], Response]] = []
        self.connections = 0
        self.drop_after_next = False
        self._next_id = 100
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                with fake._lock:
                    fake.connections += 1

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw or b"{}")
                with fake._lock:
                    fake.requests.append((self.path, body))
                    status, payload = fake._respond(self.path, body)
                    fake.responses.append((status, payload))
                    drop, fake.drop_after_next = fake.drop_after_next, False
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                if drop:
                    self.close_connection = True  # silently, without "Connection: close": a stale keep-alive

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeTelegram":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def queue(self, *responses: Response | Callable[[str, dict[str, Any]], Response]) -> None:
        self.queued.extend(responses)

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [body for path, body in self.requests if path.endswith("/" + method)]

    def _respond(self, path: str, body: dict[str, Any]) -> Response:
        if self.queued:
            nxt = self.queued.pop(0)
            return nxt(path, body) if callable(nxt) else nxt
        if path.endswith("/getMe"):
            return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "fake_bot"}}
        self._next_id += 1
        return 200, {"ok": True, "result": {"message_id": self._next_id, "date": int(time.time()),
                                            "chat": {"id": body.get("chat_id")}, "text": body.get("text")}}


def too_many_requests(retry_after: int = 7) -> Response:
    return 429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                 "parameters": {"retry_after": retry_after}}


def bad_request(description: str = "Bad Request: can't parse entities") -> Response:
    return 400, {"ok": False, "error_code": 400, "description": description}


def server_error() -> Response:
    return 502, b"<html>Bad Gateway</html>"


def alert(**overrides: Any) -> dict[str, Any]:
    base = {
        "secret": "tv-secret",  # pragma: allowlist secret
        "ticker": "BTCUSD",
        "exchange": "COINBASE",
        "interval": "5",
        "price": "97123.45",
        "bar_time": "2026-11-14T14:00:00Z",
        "fired_at": "2026-11-14T14:05:00Z",
        "msg": "Long breakout",
        "canary": False,
    }
    base.update(overrides)
    return base


def furl_event(
    body: str | bytes | dict[str, Any],
    *,
    ip: str = TV_IP,
    method: str = "POST",
    b64: bool = False,
    request_id: str = "req-1",
    time_epoch: int | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A Lambda Function URL (payload 2.0) event."""
    if isinstance(body, dict):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode()
    return {
        "version": "2.0",
        "headers": headers or {"content-type": "application/json"},
        "body": base64.b64encode(body).decode() if b64 else body.decode(),
        "isBase64Encoded": b64,
        "requestContext": {
            "requestId": request_id,
            "timeEpoch": time_epoch if time_epoch is not None else now_ms() - 5,
            "http": {"method": method, "sourceIp": ip, "path": "/"},
        },
    }


def sqs_records(sqs: Any, queue_url: str, max_messages: int = 10) -> list[dict[str, Any]]:
    """Receive from (moto) SQS and shape the messages like a Lambda SQS event's Records."""
    resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=max_messages,
                               MessageSystemAttributeNames=["All"])
    return [
        {
            "messageId": m["MessageId"],
            "receiptHandle": m["ReceiptHandle"],
            "body": m["Body"],
            "attributes": m.get("Attributes", {}),
            "eventSource": "aws:sqs",
        }
        for m in resp.get("Messages", [])
    ]


def sqs_record(body: dict[str, Any] | str, message_id: str = "m-1", **attrs: Any) -> dict[str, Any]:
    now = now_ms()
    attributes = {"SentTimestamp": str(now - 30), "ApproximateFirstReceiveTimestamp": str(now - 10),
                  "ApproximateReceiveCount": "1"}
    attributes.update({k: str(v) for k, v in attrs.items()})
    return {"messageId": message_id, "receiptHandle": f"rh-{message_id}",
            "body": body if isinstance(body, str) else json.dumps(body), "attributes": attributes}


def log_records(out: str, event: str | None = None) -> list[dict[str, Any]]:
    recs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            rec = json.loads(line)
            if event is None or rec.get("event") == event:
                recs.append(rec)
    return recs
