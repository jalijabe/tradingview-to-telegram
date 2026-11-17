"""Generate realistic mock timing records in exactly the schema the Lambdas log.

The default story: alerts arrive about 3 times an hour for 6 days. Provisioned concurrency is off for the
first half (most alerts hit two cold Lambdas) and on for the second half, so the graphs show what warm
starts buy. Raw timestamps t0..t9 are simulated, then run through the *real* ``tvtg.timing`` and
``tvtg.emf`` code, so the graph tooling works unchanged on real CloudWatch exports.

    python -m tools.mockdata --out mock --days 6 --seed 7
"""

import argparse
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tvtg import emf, timing

DAY_MS = 86_400_000
CHAT_ID = "-1001234567890"


@dataclass(frozen=True)
class Scenario:
    days: float = 6.0
    alerts_per_hour: float = 3.0
    pc_from_day: float | None = 3.0  # provisioned concurrency switched on at this day; None = never
    idle_min_s: float = 300.0  # an idle container is reclaimed after somewhere between these two
    idle_max_s: float = 900.0
    tg_keepalive_s: float = 240.0  # Telegram drops idle keep-alive connections after this long
    retry_prob: float = 0.01  # Telegram 5xx -> SQS redelivery after the visibility timeout
    visibility_ms: int = 60_000
    observer: bool = True
    observer_offset_ms: float = 4.0  # stdev of the observer's clock error (chrony-synced)
    start_ms: int = 1_793_577_600_000  # 2026-11-02T00:00:00Z


def _ln(rng: random.Random, median: float, sigma: float) -> float:
    return rng.lognormvariate(math.log(median), sigma)


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Container:
    """Rough Lambda container lifecycle: warm if used recently, or always when provisioned."""

    def __init__(self, rng: random.Random, sc: Scenario):
        self.rng, self.sc, self.last_ms = rng, sc, None

    def invoke(self, now_ms: float, provisioned: bool) -> bool:
        idle_limit = self.rng.uniform(self.sc.idle_min_s, self.sc.idle_max_s) * 1000
        cold = not provisioned and (self.last_ms is None or now_ms - self.last_ms > idle_limit)
        self.last_ms = now_ms
        return cold


def simulate(sc: Scenario, seed: int = 7) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    ingest, sender = _Container(rng, sc), _Container(rng, sc)
    last_tg_ms: float | None = None
    delivered: list[dict[str, Any]] = []
    received: list[dict[str, Any]] = []

    end_ms = sc.start_ms + sc.days * DAY_MS
    t = float(sc.start_ms)
    message_id = 1000
    while True:
        t += rng.expovariate(sc.alerts_per_hour / 3_600_000)
        if t >= end_ms:
            break
        provisioned = sc.pc_from_day is not None and t >= sc.start_ms + sc.pc_from_day * DAY_MS

        # TradingView side: 5-minute bars, the alert fires shortly after the bar closes.
        bar_open = int(t // 300_000 * 300_000)
        bar_close = bar_open + 300_000
        true_fire = bar_close + _ln(rng, 600, 0.5)
        t1 = int(true_fire // 1000 * 1000)  # {{timenow}} is truncated to the second
        tv_net = _ln(rng, 180, 0.6) * (5 if rng.random() < 0.01 else 1)  # occasional TradingView backlog
        t2 = int(true_fire + tv_net)

        ingest_cold = ingest.invoke(t2, provisioned)
        t3 = t2 + int(max(150, rng.gauss(380, 60)) if ingest_cold else _ln(rng, 6, 0.4))
        t4 = t3 + int(_ln(rng, 95, 0.3) if ingest_cold else _ln(rng, 14, 0.4))

        retried = rng.random() < sc.retry_prob
        t5 = t4 + int(_ln(rng, 25, 0.5)) + (sc.visibility_ms if retried else 0)
        sender_cold = sender.invoke(t5, provisioned)
        t6 = t5 + int(max(120, rng.gauss(330, 60)) if sender_cold else _ln(rng, 8, 0.4))
        t7 = t6 + rng.randint(0, 2)

        new_conn = sender_cold or last_tg_ms is None or t7 - last_tg_ms > sc.tg_keepalive_s * 1000
        rtt = _ln(rng, 480, 0.2) if new_conn else _ln(rng, 165, 0.15)
        t8 = t7 + int(rtt)
        last_tg_ms = t8
        message_id += 1

        ts = {"t0": bar_close, "t1": t1, "t2": t2, "t3": t3, "t4": t4, "t5": t5, "t6": t6, "t7": t7, "t8": t8}
        segs = timing.segments(ts, timing.DELIVERY_SEGMENTS)
        props = {
            "event": "delivered",
            "alert_id": f"mock-{message_id}",
            "ticker": rng.choice(("BTCUSD", "ETHUSD", "SPY")),
            "interval": "5",
            "canary": False,
            "chat_id": CHAT_ID,
            "message_id": message_id,
            **ts,
            **segs,
            "ingest_cold": ingest_cold,
            "sender_cold": sender_cold,
            "tg_new_conn": new_conn,
            "receive_count": 2 if retried else 1,
            "telegram_status": 200,
            "fallback": False,
        }
        delivered.append(emf.record({"end_to_end": segs["end_to_end"], "aws_total": segs["aws_total"]}, props, t8))

        if sc.observer:
            accepted = t8 - rtt * 0.45  # Telegram accepted it before the response travelled back
            t9 = int(accepted + _ln(rng, 110, 0.6) + rng.gauss(0, sc.observer_offset_ms))
            rts = {"t1": t1, "t2": t2, "t7": t7, "t8": t8, "t9": t9}
            rsegs = timing.segments(rts, timing.RECEPTION_SEGMENTS)
            received.append(emf.record(
                {"tg_to_client": rsegs["tg_to_client"], "tv_to_client": rsegs["tv_to_client"]},
                {"event": "received", "pk": f"{CHAT_ID}:{message_id}", **rts, **rsegs}, t9))
    return delivered, received


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="mock", type=Path)
    ap.add_argument("--days", default=6.0, type=float)
    ap.add_argument("--rate", default=3.0, type=float, help="alerts per hour")
    ap.add_argument("--pc-from-day", default=3.0, type=float, help="negative = never")
    ap.add_argument("--seed", default=7, type=int)
    args = ap.parse_args(argv)
    sc = Scenario(days=args.days, alerts_per_hour=args.rate,
                  pc_from_day=None if args.pc_from_day < 0 else args.pc_from_day)
    delivered, received = simulate(sc, args.seed)
    write_jsonl(args.out / "delivered.jsonl", delivered)
    write_jsonl(args.out / "received.jsonl", received)
    print(f"wrote {len(delivered)} delivered + {len(received)} received records to {args.out}/")


if __name__ == "__main__":
    main()
