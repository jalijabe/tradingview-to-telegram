"""Render latency graphs from timing records (mock data or a real CloudWatch export).

Input: JSONL files of ``event=delivered`` (sender) and ``event=received`` (collector) records; other lines
are ignored. Output: PNG charts plus ``summary.md`` (the table view of every number the charts show).

    python -m tools.graphs mock/delivered.jsonl mock/received.jsonl --out docs/img/mock \
        --mark 2025-11-16T00:00:00Z="provisioned concurrency on"
"""

import argparse
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from tvtg.timing import PATH_SEGMENTS  # noqa: E402

# Reference palette (light mode), fixed categorical order - see the dataviz skill's palette.md.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
SURFACE, TEXT, TEXT_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

PATH_NAMES = [name for name, _, _ in PATH_SEGMENTS]
SUMMARY_FIELDS = ("bar_to_fire", *PATH_NAMES, "aws_total", "end_to_end", "tg_to_client", "tv_to_client")


# --------------------------------------------------------------------------- data


def load(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    delivered, received = [], []
    for path in paths:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") == "delivered":
                delivered.append(rec)
            elif rec.get("event") == "received":
                received.append(rec)
    delivered.sort(key=lambda r: r.get("t8") or 0)
    return delivered, received


def join(delivered: list[dict[str, Any]], received: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach reception timings (tg_to_client, tv_to_client, t9) to their delivered record."""
    by_pk = {r["pk"]: r for r in received if "pk" in r}
    out = []
    for rec in delivered:
        rec = dict(rec)
        hit = by_pk.get(f"{rec.get('chat_id')}:{rec.get('message_id')}")
        if hit:
            for key in ("t9", "tg_to_client", "tv_to_client"):
                if key in hit:
                    rec[key] = hit[key]
        out.append(rec)
    return out


def values(records: Sequence[dict[str, Any]], field: str) -> np.ndarray:
    return np.array([r[field] for r in records if isinstance(r.get(field), int | float)], dtype=float)


def pct(arr: np.ndarray, p: float) -> float:
    return float(np.percentile(arr, p)) if arr.size else float("nan")


def retried(rec: dict[str, Any]) -> bool:
    """Redelivered by SQS: its queue_wait includes the visibility timeout, so it is charted separately."""
    return int(rec.get("receive_count") or 1) > 1


def cold_path(rec: dict[str, Any]) -> str:
    ic, sc = bool(rec.get("ingest_cold")), bool(rec.get("sender_cold"))
    return {(False, False): "warm", (True, False): "ingest cold", (False, True): "sender cold"}.get(
        (ic, sc), "both cold")


def _dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


# --------------------------------------------------------------------------- styling


def _style() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.size": 9, "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.titlecolor": TEXT, "axes.labelcolor": TEXT_2, "text.color": TEXT,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": TEXT_2, "ytick.labelcolor": TEXT_2,
        "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
        "axes.axisbelow": True, "legend.frameon": False, "lines.linewidth": 2,
        "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
    })


def _ms_axis(ax: Any, axis: str = "y") -> None:
    fmt = matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def _marks(ax: Any, marks: Sequence[tuple[datetime, str]]) -> None:
    for when, label in marks:
        ax.axvline(when, color=MUTED, linewidth=1)
        ax.annotate(label, (when, 1), xycoords=("data", "axes fraction"), xytext=(4, -4),
                    textcoords="offset points", va="top", fontsize=8, color=TEXT_2)


def _save(fig: Any, out: Path, name: str) -> Path:
    path = out / name
    fig.savefig(path, dpi=144, bbox_inches="tight")
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- charts


def chart_waterfall(records: list[dict[str, Any]], out: Path, n: int = 12) -> Path:
    """Per-alert breakdown: the slowest and the most recent alerts, one stacked bar each."""
    complete = [r for r in records if all(k in r for k in PATH_NAMES)]
    slowest = sorted(complete, key=lambda r: r.get("end_to_end", 0), reverse=True)[:n]
    latest = complete[-n:]
    fig, axes = plt.subplots(2, 1, figsize=(10, 0.32 * 2 * n + 2.2), sharex=True)
    for ax, group, title in ((axes[0], slowest, f"Slowest {len(slowest)} alerts"),
                             (axes[1], latest, f"Latest {len(latest)} alerts")):
        group = list(reversed(group))
        y = np.arange(len(group))
        left = np.zeros(len(group))
        for i, name in enumerate(PATH_NAMES):
            widths = np.array([r[name] for r in group], dtype=float)
            ax.barh(y, widths, left=left, height=0.62, color=SERIES[i], edgecolor=SURFACE, linewidth=1.5,
                    label=name)
            left += widths
        recv = [(j, r["tv_to_client"]) for j, r in enumerate(group) if "tv_to_client" in r]
        if recv:
            ax.scatter([v for _, v in recv], [j for j, _ in recv], s=36, color=TEXT, edgecolor=SURFACE,
                       linewidth=1.5, zorder=3, label="client received (t9)")
        labels = []
        for r in group:
            cold = cold_path(r)
            flags = "" if cold == "warm" else f"  [{cold}]"
            labels.append(f"{_dt(r['t8']):%b %d %H:%M}  {r.get('ticker', '')}{flags}")
        ax.set_yticks(y, labels, fontsize=8)
        ax.grid(axis="y", visible=False)
        ax.set_title(title)
        _ms_axis(ax, "x")
    axes[1].set_xlabel("ms since TradingView fired the alert (t1)")
    handles, names = axes[0].get_legend_handles_labels()
    fig.legend(handles, names, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 1.04), fontsize=8)
    fig.text(0.01, -0.02, "fire_to_edge includes up to 1 s of {{timenow}} truncation.", fontsize=8, color=MUTED)
    fig.tight_layout()
    return _save(fig, out, "waterfall.png")


def _bins(records: list[dict[str, Any]], hours: float) -> list[tuple[datetime, list[dict[str, Any]]]]:
    if not records:
        return []
    width = hours * 3_600_000
    start = records[0]["t8"] // width * width
    groups: dict[float, list[dict[str, Any]]] = {}
    for r in records:
        groups.setdefault((r["t8"] - start) // width, []).append(r)
    return [(_dt(start + k * width + width / 2), g) for k, g in sorted(groups.items())]


def chart_percentiles(records: list[dict[str, Any]], out: Path, marks: Sequence[tuple[datetime, str]],
                      hours: float = 12) -> Path:
    bins = _bins(records, hours)
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    for ax, field, title in ((axes[0], "end_to_end", "End to end (t8 − t1)"),
                             (axes[1], "aws_total", "Inside AWS (t8 − t2)")):
        xs = [x for x, _ in bins]
        for i, p in enumerate((50, 90, 99)):
            ys = [pct(values(g, field), p) for _, g in bins]
            ax.plot(xs, ys, color=SERIES[i], label=f"p{p}")
        ax.set_title(f"{title}, {hours:g} h bins")
        ax.set_ylim(bottom=0)
        _ms_axis(ax)
        _marks(ax, marks)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    axes[0].set_ylabel("ms")
    axes[0].legend(loc="upper left", bbox_to_anchor=(0, -0.12), ncol=3)
    fig.tight_layout()
    return _save(fig, out, "percentiles.png")


def chart_segments(records: list[dict[str, Any]], out: Path, marks: Sequence[tuple[datetime, str]],
                   hours: float = 12) -> Path:
    bins = _bins([r for r in records if all(k in r for k in PATH_NAMES)], hours)
    fig, ax = plt.subplots(figsize=(11, 4))
    xs = mdates.date2num([x for x, _ in bins])
    width = hours / 24 * 0.7
    bottom = np.zeros(len(bins))
    for i, name in enumerate(PATH_NAMES):
        med = np.array([pct(values(g, name), 50) for _, g in bins])
        ax.bar(xs, med, width=width, bottom=bottom, color=SERIES[i], edgecolor=SURFACE, linewidth=1.5,
               label=name)
        bottom += med
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_title(f"Where the time goes: median per segment, stacked, {hours:g} h bins")
    ax.set_ylabel("ms")
    _ms_axis(ax)
    _marks(ax, marks)
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=8)
    fig.tight_layout()
    return _save(fig, out, "segments.png")


def chart_cold(records: list[dict[str, Any]], out: Path) -> Path:
    order = ("warm", "ingest cold", "sender cold", "both cold")
    groups = [(k, values([r for r in records if cold_path(r) == k], "aws_total")) for k in order]
    groups = [(k, v) for k, v in groups if v.size]
    fig, ax = plt.subplots(figsize=(8, 3.6))
    bp = ax.boxplot([v for _, v in groups], orientation="horizontal", widths=0.5, showfliers=False, patch_artist=True,
                    medianprops={"color": SURFACE, "linewidth": 2},
                    whiskerprops={"color": SERIES[0], "linewidth": 1.5},
                    capprops={"color": SERIES[0], "linewidth": 1.5})
    for box in bp["boxes"]:
        box.set(facecolor=SERIES[0], edgecolor=SERIES[0])
    ax.set_yticks(range(1, len(groups) + 1), [f"{k}  (n={v.size})" for k, v in groups])
    for i, (_, v) in enumerate(groups, start=1):
        ax.annotate(f"p50 {pct(v, 50):,.0f}", (np.percentile(v, 75), i), xytext=(6, 9),
                    textcoords="offset points", fontsize=8, color=TEXT_2)
    ax.grid(axis="y", visible=False)
    ax.set_title("Inside-AWS latency (t8 − t2) by cold-start path")
    ax.set_xlabel("ms (box = p25–p75, whiskers = 1.5 IQR)")
    ax.set_xlim(left=0)
    _ms_axis(ax, "x")
    fig.tight_layout()
    return _save(fig, out, "cold_vs_warm.png")


def chart_cdf(records: list[dict[str, Any]], out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(8, 3.8))
    for i, (field, label) in enumerate((("aws_total", "inside AWS (t8 − t2)"),
                                        ("end_to_end", "end to end (t8 − t1)"),
                                        ("tv_to_client", "TradingView → client (t9 − t1)"))):
        v = np.sort(values(records, field))
        if v.size:
            ax.plot(v, np.arange(1, v.size + 1) / v.size * 100, color=SERIES[i], label=label)
    ax.set_title("Cumulative distribution")
    ax.set_xlabel("ms")
    ax.set_ylabel("% of alerts at or below")
    ax.set_ylim(0, 100)
    tails = [pct(values(records, f), 99.5) for f in ("aws_total", "end_to_end", "tv_to_client")]
    ax.set_xlim(left=0, right=max([t for t in tails if t == t] or [1000]) * 1.1)
    _ms_axis(ax, "x")
    ax.legend(loc="lower right")
    fig.tight_layout()
    return _save(fig, out, "cdf.png")


def chart_telegram(records: list[dict[str, Any]], out: Path) -> Path:
    reused = values([r for r in records if not r.get("tg_new_conn")], "telegram_rtt")
    fresh = values([r for r in records if r.get("tg_new_conn")], "telegram_rtt")
    top = max(pct(reused, 99.5), pct(fresh, 99.5), 1.0)
    edges = np.linspace(0, top, 40)
    fig, ax = plt.subplots(figsize=(8, 3.4))
    for i, (v, label) in enumerate(((reused, "reused connection"), (fresh, "new connection (TLS handshake)"))):
        if v.size:
            ax.hist(v, bins=edges, color=SERIES[i], edgecolor=SURFACE, linewidth=1, label=f"{label}, n={v.size}")
    ax.set_title("Telegram sendMessage round trip (t8 − t7)")
    ax.set_xlabel("ms")
    ax.set_ylabel("alerts")
    ax.grid(axis="x", visible=False)
    _ms_axis(ax, "x")
    ax.legend()
    fig.tight_layout()
    return _save(fig, out, "telegram_rtt.png")


# --------------------------------------------------------------------------- summary


def summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for field in SUMMARY_FIELDS:
        v = values(records, field)
        if v.size:
            rows.append({"segment": field, "n": int(v.size), "p50": pct(v, 50), "p90": pct(v, 90),
                         "p99": pct(v, 99), "max": float(v.max())})
    return rows


def write_summary(records: list[dict[str, Any]], out: Path, images: Sequence[Path],
                  retries: Sequence[dict[str, Any]] = ()) -> Path:
    rows = summary(records)
    n = len(records)
    counts = {k: sum(1 for r in records if cold_path(r) == k)
              for k in ("warm", "ingest cold", "sender cold", "both cold")}
    lines = ["# Latency summary", ""]
    if records:
        lines.append(f"{n} alerts, {_dt(records[0]['t8']):%Y-%m-%d %H:%M} → "
                     f"{_dt(records[-1]['t8']):%Y-%m-%d %H:%M} UTC.")
        lines.append("Cold-start paths: " + ", ".join(f"{k} {v} ({v / n:.0%})" for k, v in counts.items()) + ".")
    lines += ["", "| Segment | n | p50 ms | p90 ms | p99 ms | max ms |", "|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| `{r['segment']}` | {r['n']} | {r['p50']:,.0f} | {r['p90']:,.0f} | {r['p99']:,.0f} | "
                     f"{r['max']:,.0f} |")
    lines += ["", "`fire_to_edge` includes up to 1 s of `{{timenow}}` truncation. "
              "`bar_to_fire` is whole seconds for the same reason.", ""]
    if retries:
        lines += [f"{len(retries)} retried alerts (SQS redelivery) are left out of the table and charts above:", "",
                  "| Alert | Delivered (UTC) | Attempts | queue_wait ms | end_to_end ms |", "|---|---|---:|---:|---:|"]
        for r in retries:
            lines.append(f"| `{r.get('alert_id')}` | {_dt(r['t8']):%Y-%m-%d %H:%M:%S} | {r.get('receive_count')} | "
                         f"{r.get('queue_wait', 0):,.0f} | {r.get('end_to_end', 0):,.0f} |")
        lines.append("")
    lines += [f"![{p.stem}]({p.name})" for p in images]
    path = out / "summary.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def render_all(paths: Iterable[Path], out: Path, marks: Sequence[tuple[datetime, str]] = ()) -> list[Path]:
    delivered, received = load(paths)
    if not delivered:
        raise SystemExit("no event=delivered records found")
    joined = join(delivered, received)
    records = [r for r in joined if not retried(r)]
    retries = [r for r in joined if retried(r)]
    out.mkdir(parents=True, exist_ok=True)
    _style()
    images = [
        chart_waterfall(records, out),
        chart_percentiles(records, out, marks),
        chart_segments(records, out, marks),
        chart_cold(records, out),
        chart_cdf(records, out),
        chart_telegram(records, out),
    ]
    return [*images, write_summary(records, out, images, retries)]


def _parse_mark(text: str) -> tuple[datetime, str]:
    when, _, label = text.partition("=")
    return datetime.fromisoformat(when.replace("Z", "+00:00")), label or when


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--out", default=Path("out/graphs"), type=Path)
    ap.add_argument("--mark", action="append", default=[], help="ISO_TIME=label vertical marker (repeatable)")
    args = ap.parse_args(argv)
    for path in render_all(args.inputs, args.out, [_parse_mark(m) for m in args.mark]):
        print(path)


if __name__ == "__main__":
    main()
