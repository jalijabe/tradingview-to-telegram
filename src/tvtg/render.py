"""Turn an alert dict into Telegram message text."""

from html import escape
from typing import Any

TELEGRAM_LIMIT = 4096
_MSG_LIMIT = 3500  # leaves room for the header lines and HTML tags


def _s(alert: dict[str, Any], key: str) -> str:
    value = alert.get(key)
    return "" if value is None else str(value).strip()


def _parts(alert: dict[str, Any]) -> dict[str, str]:
    msg = _s(alert, "msg")
    if len(msg) > _MSG_LIMIT:
        msg = msg[: _MSG_LIMIT - 1] + "…"
    return {
        "ticker": _s(alert, "ticker") or "?",
        "exchange": _s(alert, "exchange"),
        "interval": _s(alert, "interval"),
        "price": _s(alert, "price"),
        "fired_at": _s(alert, "fired_at"),
        "msg": msg,
        "canary": "1" if alert.get("canary") is True else "",
    }


def render_html(alert: dict[str, Any]) -> str:
    p = {k: escape(v, quote=False) for k, v in _parts(alert).items()}
    head = f"<b>{p['ticker']}</b>"
    if p["exchange"]:
        head += f" · {p['exchange']}"
    if p["interval"]:
        head += f" · {p['interval']}"
    if p["canary"]:
        head = "[canary] " + head
    lines = [head]
    if p["msg"]:
        lines.append(p["msg"])
    if p["price"]:
        lines.append(f"Price: <code>{p['price']}</code>")
    if p["fired_at"]:
        lines.append(f"<i>{p['fired_at']}</i>")
    return "\n".join(lines)[:TELEGRAM_LIMIT]


def render_plain(alert: dict[str, Any]) -> str:
    p = _parts(alert)
    head = " · ".join(x for x in (p["ticker"], p["exchange"], p["interval"]) if x)
    if p["canary"]:
        head = "[canary] " + head
    lines = [head]
    if p["msg"]:
        lines.append(p["msg"])
    if p["price"]:
        lines.append(f"Price: {p['price']}")
    if p["fired_at"]:
        lines.append(p["fired_at"])
    return "\n".join(lines)[:TELEGRAM_LIMIT]
