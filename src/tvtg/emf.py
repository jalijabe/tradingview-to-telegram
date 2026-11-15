"""CloudWatch Embedded Metric Format records.

Only the metric names passed in ``metrics`` become (paid) custom metrics; everything in ``props`` stays a
plain log field that Logs Insights can query.
"""

from typing import Any

NAMESPACE = "TvTgBot"


def record(
    metrics: dict[str, int | float | None],
    props: dict[str, Any],
    timestamp_ms: int,
    namespace: str = NAMESPACE,
) -> dict[str, Any]:
    present = {k: v for k, v in metrics.items() if v is not None}
    out: dict[str, Any] = dict(props)
    out.update(present)
    if present:
        out["_aws"] = {
            "Timestamp": int(timestamp_ms),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [[]],
                    "Metrics": [{"Name": name, "Unit": "Milliseconds"} for name in present],
                }
            ],
        }
    return out
