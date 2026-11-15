import copy
import json

import numpy as np
import pytest

from tools import export_logs, graphs, mockdata
from tvtg.timing import PATH_SEGMENTS

HOPS = [name for name, _, _ in PATH_SEGMENTS]


@pytest.fixture(scope="module")
def mock_records():
    return mockdata.simulate(mockdata.Scenario(days=2, pc_from_day=1), seed=3)


# ----------------------------------------------------------------------------- mockdata


def test_mock_is_deterministic():
    sc = mockdata.Scenario(days=0.5)
    assert mockdata.simulate(sc, seed=1) == mockdata.simulate(sc, seed=1)
    assert mockdata.simulate(sc, seed=1) != mockdata.simulate(sc, seed=2)


def test_mock_records_match_the_sender_schema(mock_records):
    delivered, received = mock_records
    assert delivered and len(received) == len(delivered)
    required = {"event", "alert_id", "ticker", "chat_id", "message_id", "ingest_cold", "sender_cold",
                "tg_new_conn", "receive_count", "telegram_status", "aws_total", "end_to_end", "_aws",
                *(f"t{i}" for i in range(9)), *HOPS}
    for d in delivered:
        assert required <= d.keys()
        assert sum(d[h] for h in HOPS) == d["end_to_end"]
        assert all(d[h] >= 0 for h in HOPS)
        assert d["t1"] % 1000 == 0  # {{timenow}} truncation is modelled
    pks = {f"{d['chat_id']}:{d['message_id']}" for d in delivered}
    assert {r["pk"] for r in received} == pks


def test_provisioned_concurrency_removes_cold_starts(mock_records):
    delivered, _ = mock_records
    split = mockdata.Scenario().start_ms + mockdata.DAY_MS
    before = [d for d in delivered if d["t2"] < split]
    after = [d for d in delivered if d["t2"] >= split]
    assert sum(d["ingest_cold"] for d in before) / len(before) > 0.3
    assert not any(d["ingest_cold"] or d["sender_cold"] for d in after)
    assert np.median([d["aws_total"] for d in after]) < np.median([d["aws_total"] for d in before])


def test_no_observer_scenario():
    _, received = mockdata.simulate(mockdata.Scenario(days=0.2, observer=False))
    assert received == []


def test_mock_cli_writes_jsonl(tmp_path, capsys):
    mockdata.main(["--out", str(tmp_path), "--days", "0.5", "--pc-from-day", "-1", "--seed", "4"])
    lines = (tmp_path / "delivered.jsonl").read_text().splitlines()
    assert lines and all(json.loads(line)["event"] == "delivered" for line in lines)
    assert (tmp_path / "received.jsonl").exists()
    assert "wrote" in capsys.readouterr().out


# ----------------------------------------------------------------------------- graphs


def test_load_ignores_noise_and_sorts(tmp_path):
    f = tmp_path / "mixed.log"
    f.write_text("\n".join([
        "START RequestId: abc",
        '{"event": "delivered", "t8": 20, "chat_id": 1, "message_id": 2}',
        "{broken json",
        '{"event": "keepalive"}',
        '{"event": "delivered", "t8": 10, "chat_id": 1, "message_id": 1}',
        '{"event": "received", "pk": "1:2", "t9": 25, "tg_to_client": 5, "tv_to_client": 9}',
    ]))
    delivered, received = graphs.load([f])
    assert [d["t8"] for d in delivered] == [10, 20] and len(received) == 1
    joined = graphs.join(delivered, received)
    assert "tg_to_client" not in joined[0] and joined[1]["tv_to_client"] == 9


def test_percentile_and_values():
    recs = [{"x": v} for v in range(1, 101)] + [{"x": None}, {"y": 1}, {"x": "7"}]
    v = graphs.values(recs, "x")
    assert v.size == 100
    assert graphs.pct(v, 50) == pytest.approx(50.5)
    assert np.isnan(graphs.pct(np.array([]), 50))


@pytest.mark.parametrize(("ic", "sc", "label"), [(False, False, "warm"), (True, False, "ingest cold"),
                                                  (False, True, "sender cold"), (True, True, "both cold")])
def test_cold_path(ic, sc, label):
    assert graphs.cold_path({"ingest_cold": ic, "sender_cold": sc}) == label


def test_render_all_from_mock(tmp_path, mock_records):
    delivered, received = copy.deepcopy(mock_records)
    delivered[5]["receive_count"] = 2  # force at least one retried alert
    n_retried = sum(1 for d in delivered if d["receive_count"] > 1)
    src = tmp_path / "in.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in delivered + received))
    marks = [graphs._parse_mark("2026-11-03T00:00:00Z=PC on")]
    outputs = graphs.render_all([src], tmp_path / "out", marks)
    names = {p.name for p in outputs}
    assert names == {"waterfall.png", "percentiles.png", "segments.png", "cold_vs_warm.png", "cdf.png",
                     "telegram_rtt.png", "summary.md"}
    for p in outputs:
        if p.suffix == ".png":
            assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and p.stat().st_size > 10_000
    summary = (tmp_path / "out" / "summary.md").read_text()
    assert f"{len(delivered) - n_retried} alerts" in summary
    assert f"{n_retried} retried alerts" in summary and delivered[5]["alert_id"] in summary
    for field in ("fire_to_edge", "telegram_rtt", "tv_to_client"):
        assert f"`{field}`" in summary


def test_summary_rows_are_ordered_percentiles(mock_records):
    rows = graphs.summary(graphs.join(*mock_records))
    assert rows and all(r["p50"] <= r["p90"] <= r["p99"] <= r["max"] for r in rows)


def test_render_without_reception_data(tmp_path, mock_records):
    src = tmp_path / "d.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in mock_records[0]))
    graphs.render_all([src], tmp_path / "out")
    assert "`tv_to_client`" not in (tmp_path / "out" / "summary.md").read_text()


def test_render_with_no_records_fails(tmp_path):
    empty = tmp_path / "e.jsonl"
    empty.write_text("")
    with pytest.raises(SystemExit):
        graphs.render_all([empty], tmp_path / "out")


def test_graphs_cli(tmp_path, mock_records, capsys):
    src = tmp_path / "d.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in mock_records[0]))
    graphs.main([str(src), "--out", str(tmp_path / "g"), "--mark", "2026-11-03T00:00:00Z"])
    assert "summary.md" in capsys.readouterr().out


# ----------------------------------------------------------------------------- export_logs


class FakeLogs:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        group = kwargs["logGroupName"]
        idx = int(kwargs.get("nextToken", 0))
        events, more = self.pages.get(group, [[]])[idx], idx + 1 < len(self.pages.get(group, [[]]))
        return {"events": [{"message": m} for m in events], **({"nextToken": str(idx + 1)} if more else {})}


def test_export_follows_pagination_and_parses_lines(tmp_path):
    logs = FakeLogs({
        "/aws/lambda/tvtg-dev-sender": [
            ['{"event":"delivered","t8":1}', "2026-11-14T00:00:00Z abc INFO not json"],
            ['prefix {"event":"delivered","t8":2}', "{bad"],
        ],
        "/aws/lambda/tvtg-dev-collector": [['{"event":"received","pk":"1:1"}']],
    })
    counts = export_logs.export(logs, "dev", 2, tmp_path, now_ms=10_000_000)
    assert counts == {"delivered": 2, "received": 1}
    assert logs.calls[0]["filterPattern"] == '{ $.event = "delivered" }'
    assert logs.calls[0]["startTime"] == 10_000_000 - 7_200_000 and logs.calls[1]["nextToken"] == "1"
    lines = (tmp_path / "delivered.jsonl").read_text().splitlines()
    assert [json.loads(line)["t8"] for line in lines] == [1, 2]
