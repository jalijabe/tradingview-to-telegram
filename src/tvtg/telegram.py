"""Minimal Telegram Bot API client that keeps its HTTPS connection alive between invocations.

A fresh TLS handshake to api.telegram.org costs several transatlantic round trips from us-west-2, so the
connection is reused across warm invocations. If a reused connection turns out to be stale, the request is
retried once on a fresh connection.
"""

import http.client
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class TgResult:
    status: int
    body: dict[str, Any]
    new_conn: bool

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.body.get("ok"))

    @property
    def retry_after(self) -> int | None:
        value = (self.body.get("parameters") or {}).get("retry_after")
        return int(value) if isinstance(value, int | float) else None


class TelegramClient:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org", timeout_s: float = 10.0):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"bad Telegram base URL: {base_url!r}")
        self._token = token
        self._https = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self._timeout = timeout_s
        self._conn: http.client.HTTPConnection | None = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connect(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=self._timeout)

    def call(self, method: str, payload: dict[str, Any]) -> TgResult:
        data = json.dumps(payload).encode()
        path = f"{self._prefix}/bot{self._token}/{method}"
        headers = {"Content-Type": "application/json"}
        for _ in range(2):
            new_conn = self._conn is None
            if self._conn is None:
                self._conn = self._connect()
            try:
                self._conn.request("POST", path, body=data, headers=headers)
                resp = self._conn.getresponse()
                raw = resp.read()
            except (http.client.HTTPException, OSError):
                self.close()
                if new_conn:
                    raise
                continue  # the reused connection was stale; retry once on a fresh one
            if resp.will_close:
                self.close()
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                body = {"ok": False, "description": raw[:200].decode(errors="replace")}
            return TgResult(resp.status, body, new_conn)
        raise RuntimeError("unreachable")  # pragma: no cover

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> TgResult:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if disable_notification:
            payload["disable_notification"] = True
        return self.call("sendMessage", payload)
