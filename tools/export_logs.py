"""Export real timing records from CloudWatch Logs to JSONL, for tools/graphs.py.

    python -m tools.export_logs --env dev --hours 72 --out export/
    python -m tools.graphs export/delivered.jsonl export/received.jsonl --out out/graphs

Needs AWS credentials with logs:FilterLogEvents on the two Lambda log groups.
"""

import argparse
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def iter_events(logs: Any, group: str, pattern: str, start_ms: int, end_ms: int) -> Iterator[dict[str, Any]]:
    """Yield the JSON records whose log lines match ``pattern``, following pagination."""
    kwargs: dict[str, Any] = {"logGroupName": group, "filterPattern": pattern,
                              "startTime": start_ms, "endTime": end_ms}
    while True:
        page = logs.filter_log_events(**kwargs)
        for ev in page.get("events", []):
            msg = ev.get("message", "").strip()
            start = msg.find("{")
            if start < 0:
                continue
            try:
                yield json.loads(msg[start:])
            except ValueError:
                continue
        token = page.get("nextToken")
        if not token:
            return
        kwargs["nextToken"] = token


def export(logs: Any, env: str, hours: float, out: Path, now_ms: int | None = None) -> dict[str, int]:
    end = now_ms if now_ms is not None else int(time.time() * 1000)
    start = end - int(hours * 3_600_000)
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, fn, event in (("delivered", "sender", "delivered"), ("received", "collector", "received")):
        group = f"/aws/lambda/tvtg-{env}-{fn}"
        pattern = f'{{ $.event = "{event}" }}'
        n = 0
        with (out / f"{name}.jsonl").open("w") as fh:
            for rec in iter_events(logs, group, pattern, start, end):
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                n += 1
        counts[name] = n
    return counts


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - needs AWS
    import boto3

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default="dev")
    ap.add_argument("--hours", default=72.0, type=float)
    ap.add_argument("--out", default=Path("export"), type=Path)
    args = ap.parse_args(argv)
    print(export(boto3.client("logs"), args.env, args.hours, args.out))


if __name__ == "__main__":  # pragma: no cover
    main()
