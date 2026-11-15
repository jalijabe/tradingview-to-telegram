"""One JSON object per line on stdout; Lambda ships stdout to CloudWatch Logs."""

import json
from typing import Any


def emit(record: dict[str, Any]) -> None:
    print(json.dumps(record, separators=(",", ":"), default=str), flush=True)


def log(event: str, **fields: Any) -> None:
    emit({"event": event, **fields})
