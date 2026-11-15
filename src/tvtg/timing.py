"""Timestamp parsing and the segment model from docs/design.md section 4.

Timestamps are epoch milliseconds:
  t0 bar close, t1 TradingView fired, t2 AWS edge, t3 ingest start, t4 queued, t5 dequeued,
  t6 sender start, t7 Telegram request, t8 Telegram response, t9 client received (observer).
"""

import re
from datetime import UTC, datetime

# (name, from, to). Order matters for the waterfall: these are the consecutive hops.
PATH_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("fire_to_edge", "t1", "t2"),
    ("edge_to_ingest", "t2", "t3"),
    ("ingest_to_queue", "t3", "t4"),
    ("queue_wait", "t4", "t5"),
    ("dequeue_to_sender", "t5", "t6"),
    ("sender_prep", "t6", "t7"),
    ("telegram_rtt", "t7", "t8"),
)

DELIVERY_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("bar_to_fire", "t0", "t1"),
    *PATH_SEGMENTS,
    ("aws_total", "t2", "t8"),
    ("end_to_end", "t1", "t8"),
)

RECEPTION_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("tg_to_client", "t7", "t9"),
    ("tv_to_client", "t1", "t9"),
)

_INTERVAL_RE = re.compile(r"(\d+)(S?)")


def iso_to_ms(value: object) -> int | None:
    """Parse a TradingView ``{{time}}`` / ``{{timenow}}`` string (ISO-8601, UTC) to epoch ms."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def interval_ms(value: object) -> int | None:
    """Length of a TradingView ``{{interval}}`` in ms.

    Minutes are bare numbers ("1", "60", "240") and seconds end in "S" ("30S").
    Daily/weekly/monthly ("1D", "W", "1M") and tick/range intervals have no fixed length here -> None.
    """
    m = _INTERVAL_RE.fullmatch(str(value or "").strip().upper())
    if not m or int(m.group(1)) == 0:
        return None
    n = int(m.group(1))
    return n * 1000 if m.group(2) else n * 60_000


def bar_close_ms(bar_time: object, interval: object) -> int | None:
    """t0: ``{{time}}`` is the bar *open*, so close = open + interval (intraday only)."""
    start, length = iso_to_ms(bar_time), interval_ms(interval)
    if start is None or length is None:
        return None
    return start + length


def segments(ts: dict[str, int | None], spec: tuple[tuple[str, str, str], ...]) -> dict[str, int]:
    """Durations for every segment whose two endpoints are both known."""
    out: dict[str, int] = {}
    for name, start, end in spec:
        a, b = ts.get(start), ts.get(end)
        if a is not None and b is not None:
            out[name] = int(b) - int(a)
    return out
