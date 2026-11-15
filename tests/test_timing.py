import pytest

from tvtg import timing


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-14T14:05:00Z", 1_794_665_100_000),
        ("2026-11-14T14:05:00+00:00", 1_794_665_100_000),
        ("2026-11-14T14:05:00", 1_794_665_100_000),  # naive -> UTC
        ("2026-11-14T09:05:00-05:00", 1_794_665_100_000),
        ("2026-11-14T14:05:00.250Z", 1_794_665_100_250),
        ("  2026-11-14T14:05:00Z ", 1_794_665_100_000),
    ],
)
def test_iso_to_ms_parses_tradingview_formats(value, expected):
    assert timing.iso_to_ms(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "{{timenow}}", "yesterday", 12345, {"x": 1}])
def test_iso_to_ms_rejects_garbage(value):
    assert timing.iso_to_ms(value) is None


@pytest.mark.parametrize(
    ("interval", "expected"),
    [("1", 60_000), ("5", 300_000), ("60", 3_600_000), ("240", 14_400_000), ("30S", 30_000), ("1s", 1_000),
     (" 15 ", 900_000), (5, 300_000)],
)
def test_interval_ms_intraday(interval, expected):
    assert timing.interval_ms(interval) == expected


@pytest.mark.parametrize("interval", ["1D", "D", "1W", "W", "1M", "M", "100T", "1R", "", None, "0", "abc"])
def test_interval_ms_without_fixed_length(interval):
    assert timing.interval_ms(interval) is None


def test_bar_close_is_open_plus_interval():
    assert timing.bar_close_ms("2026-11-14T14:00:00Z", "5") == timing.iso_to_ms("2026-11-14T14:05:00Z")
    assert timing.bar_close_ms("2026-11-14T14:00:00Z", "1D") is None
    assert timing.bar_close_ms("not a time", "5") is None


def test_segments_skip_unknown_endpoints():
    ts = {"t0": None, "t1": 1000, "t2": 1250, "t3": 1260, "t8": 1700}
    segs = timing.segments(ts, timing.DELIVERY_SEGMENTS)
    assert segs == {"fire_to_edge": 250, "edge_to_ingest": 10, "aws_total": 450, "end_to_end": 700}


def test_path_segments_chain_end_to_end():
    """The consecutive hops must add up exactly to t8 - t1 (the waterfall relies on it)."""
    ts = {f"t{i}": 1000 + i * 37 + i * i for i in range(10)}
    segs = timing.segments(ts, timing.DELIVERY_SEGMENTS)
    hops = [name for name, _, _ in timing.PATH_SEGMENTS]
    assert sum(segs[h] for h in hops) == segs["end_to_end"]
    assert [a for _, a, _ in timing.PATH_SEGMENTS][1:] == [b for _, _, b in timing.PATH_SEGMENTS][:-1]


def test_reception_segments():
    segs = timing.segments({"t1": 1000, "t7": 1500, "t9": 1720}, timing.RECEPTION_SEGMENTS)
    assert segs == {"tg_to_client": 220, "tv_to_client": 720}
