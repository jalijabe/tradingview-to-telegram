import json

from tests.fakes import log_records
from tvtg import emf, log
from tvtg.render import TELEGRAM_LIMIT, render_html, render_plain

# ----------------------------------------------------------------------------- EMF


def test_emf_record_declares_only_present_metrics():
    rec = emf.record({"end_to_end": 900, "aws_total": None}, {"event": "delivered", "ticker": "BTC"}, 123)
    directive = rec["_aws"]["CloudWatchMetrics"][0]
    assert rec["_aws"]["Timestamp"] == 123
    assert directive["Namespace"] == "TvTgBot"
    assert directive["Dimensions"] == [[]]
    assert directive["Metrics"] == [{"Name": "end_to_end", "Unit": "Milliseconds"}]
    # Every declared metric must exist as a top-level member, or CloudWatch drops the record.
    for metric in directive["Metrics"]:
        assert isinstance(rec[metric["Name"]], int | float)
    assert rec["event"] == "delivered" and rec["ticker"] == "BTC"
    assert "aws_total" not in rec


def test_emf_record_without_metrics_is_a_plain_log_line():
    rec = emf.record({"x": None}, {"event": "received"}, 1)
    assert "_aws" not in rec


def test_log_emits_one_json_object_per_line(capsys):
    log.log("rejected", reason="ip", n=1)
    log.emit({"event": "x", "nested": {"a": [1, 2]}, "obj": object()})  # default=str for odd values
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"event": "rejected", "reason": "ip", "n": 1}
    assert log_records("\n".join(lines), "x")[0]["nested"] == {"a": [1, 2]}


# ----------------------------------------------------------------------------- render


def test_render_html_escapes_user_text():
    text = render_html({"ticker": "A<B", "msg": "x & <script>", "price": "1 > 0", "fired_at": "t"})
    assert "<b>A&lt;B</b>" in text
    assert "x &amp; &lt;script&gt;" in text
    assert "<code>1 &gt; 0</code>" in text
    assert "<script>" not in text


def test_render_html_layout():
    text = render_html({"ticker": "BTCUSD", "exchange": "COINBASE", "interval": "5", "msg": "Long",
                        "price": "97000", "fired_at": "2026-11-14T14:05:00Z"})
    assert text.splitlines() == ["<b>BTCUSD</b> · COINBASE · 5", "Long", "Price: <code>97000</code>",
                                 "<i>2026-11-14T14:05:00Z</i>"]


def test_render_canary_and_missing_fields():
    assert render_html({"canary": True}) == "[canary] <b>?</b>"
    assert render_plain({"canary": "true"}) == "?"  # only a real boolean counts as canary
    assert render_plain({"ticker": None, "msg": None}) == "?"


def test_render_plain_has_no_markup():
    text = render_plain({"ticker": "A<B", "exchange": "X", "msg": "m & n", "price": "1", "fired_at": "t"})
    assert text == "A<B · X\nm & n\nPrice: 1\nt"


def test_render_truncates_long_messages():
    long_msg = "<" * 10_000  # escapes to 4x the length
    for text in (render_html({"msg": long_msg}), render_plain({"msg": long_msg})):
        assert len(text) <= TELEGRAM_LIMIT
