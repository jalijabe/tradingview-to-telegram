import asyncio
import io
import json
import subprocess
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from observer import observer

KEY = b"k"

BASE_ENV = {
    "TG_API_ID": "12345",
    "TG_API_HASH": "abc",
    "TG_SESSION": "1SESSION",
    "CHAT_IDS": "-1001, -1002",
    "COLLECTOR_URL": "https://collector.example/",
    "COLLECTOR_KEY": "ck",
    "OBSERVER_NAME": "pi-home",
}


# ----------------------------------------------------------------------------- post_report


class FakeResp(io.BytesIO):
    def __init__(self, status):
        super().__init__(b"")
        self.status = status


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


def scripted(*outcomes):
    calls = []

    def urlopen(req, timeout):
        calls.append(req)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResp(outcome)

    return urlopen, calls


def test_success_first_try():
    urlopen, calls = scripted(200)
    sleeps = []
    assert observer.post_report("http://x", KEY, b"{}", urlopen=urlopen, sleep=sleeps.append) == 200
    assert len(calls) == 1 and sleeps == []
    req = calls[0]
    assert req.get_method() == "POST" and req.get_header("X-sig") == observer.sign(KEY, b"{}")


def test_retries_404_until_stored():
    urlopen, calls = scripted(http_error(404), http_error(404), 200)
    sleeps = []
    status = observer.post_report("http://x", KEY, b"{}", urlopen=urlopen, sleep=sleeps.append, retry_delay_s=1.5)
    assert status == 200 and len(calls) == 3 and sleeps == [1.5, 1.5]


def test_retries_5xx_and_network_errors():
    urlopen, _ = scripted(http_error(503), urllib.error.URLError("down"), 200)
    assert observer.post_report("http://x", KEY, b"{}", urlopen=urlopen, sleep=lambda s: None) == 200


def test_gives_up_after_retries():
    urlopen, calls = scripted(http_error(404), http_error(404), http_error(404))
    assert observer.post_report("http://x", KEY, b"{}", urlopen=urlopen, sleep=lambda s: None) == 404
    assert len(calls) == 3


def test_network_failure_every_time_returns_zero():
    urlopen, _ = scripted(OSError(), OSError(), OSError())
    assert observer.post_report("http://x", KEY, b"{}", urlopen=urlopen, sleep=lambda s: None) == 0


@pytest.mark.parametrize("code", [400, 401, 403])
def test_client_errors_are_not_retried(code):
    urlopen, calls = scripted(http_error(code))
    assert observer.post_report("http://x", KEY, b"{}", urlopen=urlopen, sleep=lambda s: None) == code
    assert len(calls) == 1


def test_against_a_real_http_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers["X-Sig"], body))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = observer.build_report(-100, 1, 2, "pi")
        assert observer.post_report(f"http://127.0.0.1:{server.server_address[1]}/", KEY, body) == 200
    finally:
        server.shutdown()
        server.server_close()
    sig, got = received[0]
    assert got == body and sig == observer.sign(KEY, body)
    assert json.loads(got) == {"chat_id": -100, "message_id": 1, "recv_ms": 2, "observer": "pi"}


def test_report_without_observer_label_and_truncation():
    assert "observer" not in json.loads(observer.build_report(1, 2, 3))
    assert len(json.loads(observer.build_report(1, 2, 3, "x" * 100))["observer"]) == 64


def test_now_ms_is_epoch_ms():
    assert 1_700_000_000_000 < observer.now_ms() < 5_000_000_000_000


# ----------------------------------------------------------------------------- message handler


def test_handler_stamps_first_then_reports():
    cfg = observer.load_config(BASE_ENV)
    order, posted, printed = [], [], []

    async def fake_sleep(s):
        order.append(("sleep", s))

    def fake_post(url, key, body):
        posted.append((url, key, json.loads(body)))
        return 200

    def clock():
        order.append("clock")
        return 1_000

    handler = observer.make_handler(cfg, post=fake_post, sleep=fake_sleep, clock=clock, out=printed.append)
    event = SimpleNamespace(chat_id=-1001, message=SimpleNamespace(id=55))
    assert asyncio.run(handler(event)) == 200
    assert order == ["clock", ("sleep", observer.REPORT_DELAY_S)]  # t9 is captured before waiting
    assert posted == [("https://collector.example/", b"ck",
                       {"chat_id": -1001, "message_id": 55, "recv_ms": 1_000, "observer": "pi-home"})]
    assert json.loads(printed[0])["status"] == 200


# ----------------------------------------------------------------------------- configuration


def test_load_config_from_env():
    cfg = observer.load_config(BASE_ENV)
    assert cfg.api_id == 12345 and cfg.chat_ids == [-1001, -1002] and cfg.collector_key == b"ck"
    assert cfg.session == "1SESSION" and cfg.name == "pi-home"
    assert cfg.sources == {"TG_API_ID": "env", "TG_API_HASH": "env", "TG_SESSION": "env", "COLLECTOR_KEY": "env"}


def test_name_defaults_to_hostname(monkeypatch):
    monkeypatch.setattr(observer.socket, "gethostname", lambda: "raspberrypi")
    env = {k: v for k, v in BASE_ENV.items() if k != "OBSERVER_NAME"}
    assert observer.load_config(env).name == "raspberrypi"


@pytest.mark.parametrize("drop", ["TG_API_ID", "TG_API_HASH", "CHAT_IDS", "COLLECTOR_URL", "COLLECTOR_KEY"])
def test_missing_setting(drop):
    env = {k: v for k, v in BASE_ENV.items() if k != drop}
    with pytest.raises(observer.ConfigError, match=drop):
        observer.load_config(env)


def test_placeholder_counts_as_missing():
    with pytest.raises(observer.ConfigError, match="TG_API_HASH"):
        observer.load_config({**BASE_ENV, "TG_API_HASH": "CHANGE_ME"})


@pytest.mark.parametrize("override", [{"TG_API_ID": "abc"}, {"CHAT_IDS": "-100x"}, {"CHAT_IDS": " , "}])
def test_bad_values(override):
    with pytest.raises(observer.ConfigError):
        observer.load_config({**BASE_ENV, **override})


def test_login_needs_only_api_credentials():
    cfg = observer.load_config({"TG_API_ID": "1", "TG_API_HASH": "h"}, purpose="login")
    assert cfg.api_id == 1 and cfg.chat_ids == []


def test_session_file_fallback(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k != "TG_SESSION"}
    with pytest.raises(observer.ConfigError, match="no Telegram session"):
        observer.load_config({**env, "SESSION_PATH": str(tmp_path / "observer.session")})
    (tmp_path / "observer.session").write_bytes(b"sqlite")
    cfg = observer.load_config({**env, "SESSION_PATH": str(tmp_path / "observer.session")})
    assert cfg.session is None and cfg.sources["session"].startswith("file:")
    # Telethon appends ".session" when the path has no extension
    cfg = observer.load_config({**env, "SESSION_PATH": str(tmp_path / "observer")})
    assert cfg.session_path == str(tmp_path / "observer")


def test_session_objects(tmp_path):
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    s = StringSession()
    s.set_dc(2, "149.154.167.51", 443)
    s.auth_key = AuthKey(bytes(256))
    cfg = observer.load_config({**BASE_ENV, "TG_SESSION": s.save()})
    assert isinstance(observer._session(cfg), StringSession)
    file_cfg = observer.Config(api_id=1, api_hash="h", session=None, session_path="x.session")
    assert observer._session(file_cfg) == "x.session"


SSM_ENV = {
    "CHAT_IDS": "-1001",
    "COLLECTOR_URL": "https://collector.example/",
    "OBSERVER_SSM_PREFIX": "/tvtg-prod/observer/",
    "AWS_REGION": "us-east-2",
    "COLLECTOR_KEY_SSM": "/tvtg-prod/collector_key",
    "COLLECTOR_KEY_SSM_REGION": "us-west-2",
}


@pytest.fixture
def ssm_secrets():
    """The layout Terraform creates for observer_host = "ec2": observer params in the observer's region,
    the collector key in the pipeline's region."""
    with mock_aws():
        east = boto3.client("ssm", region_name="us-east-2")
        for name, value in (("api_id", "777"), ("api_hash", "hash-from-ssm"), ("session", "1SSMSESSION")):
            east.put_parameter(Name=f"/tvtg-prod/observer/{name}", Value=value, Type="SecureString")
        west = boto3.client("ssm", region_name="us-west-2")
        west.put_parameter(Name="/tvtg-prod/collector_key", Value="key-from-ssm", Type="SecureString")
        yield east


def test_secrets_from_ssm_across_regions(ssm_secrets):
    cfg = observer.load_config(SSM_ENV)
    assert (cfg.api_id, cfg.api_hash, cfg.session, cfg.collector_key) == (777, "hash-from-ssm", "1SSMSESSION",
                                                                            b"key-from-ssm")
    assert cfg.sources["TG_SESSION"] == "ssm:/tvtg-prod/observer/session"
    assert cfg.sources["COLLECTOR_KEY"] == "ssm:/tvtg-prod/collector_key"


def test_env_overrides_ssm(ssm_secrets):
    cfg = observer.load_config({**SSM_ENV, "TG_API_HASH": "local"})
    assert cfg.api_hash == "local" and cfg.sources["TG_API_HASH"] == "env"


def test_ssm_placeholder_reports_missing(ssm_secrets):
    ssm_secrets.put_parameter(Name="/tvtg-prod/observer/session", Value="CHANGE_ME", Type="SecureString",
                              Overwrite=True)
    with pytest.raises(observer.ConfigError, match="no Telegram session"):
        observer.load_config(SSM_ENV)
    ssm_secrets.put_parameter(Name="/tvtg-prod/observer/api_hash", Value="CHANGE_ME", Type="SecureString",
                              Overwrite=True)
    with pytest.raises(observer.ConfigError, match="TG_API_HASH.*CHANGE_ME"):
        observer.load_config(SSM_ENV)


def test_missing_ssm_parameter(ssm_secrets):
    with pytest.raises(observer.ConfigError, match="cannot read SSM parameter /tvtg-prod/nope/api_id"):
        observer.load_config({**SSM_ENV, "OBSERVER_SSM_PREFIX": "/tvtg-prod/nope"})


def test_injected_ssm_getter():
    seen = []

    def getter(name, region):
        seen.append((name, region))
        return {"/p/api_id": "1", "/p/api_hash": "h", "/p/session": "s"}[name]

    cfg = observer.load_config({"OBSERVER_SSM_PREFIX": "/p", "AWS_DEFAULT_REGION": "eu-west-1",
                                "CHAT_IDS": "1", "COLLECTOR_URL": "u", "COLLECTOR_KEY": "k"}, ssm_get=getter)
    assert cfg.session == "s" and seen == [("/p/api_id", "eu-west-1"), ("/p/api_hash", "eu-west-1"),
                                           ("/p/session", "eu-west-1")]


def test_read_env_file(tmp_path):
    f = tmp_path / "observer.env"
    f.write_text("# comment\n\nTG_API_ID=1\nTG_API_HASH = 'quoted'\nCOLLECTOR_URL=\"https://x/?a=b\"\n"
                 "EMPTY=\nnot a setting\n")
    assert observer.read_env_file(str(f)) == {"TG_API_ID": "1", "TG_API_HASH": "quoted",
                                              "COLLECTOR_URL": "https://x/?a=b", "EMPTY": ""}


def test_example_env_file_parses_and_documents_every_setting():
    values = observer.read_env_file("observer/observer.env.example")
    for key in ("OBSERVER_NAME", "CHAT_IDS", "COLLECTOR_URL", "TG_API_ID", "TG_API_HASH", "TG_SESSION",
                "COLLECTOR_KEY"):
        assert key in values
    text = open("observer/observer.env.example").read()
    for key in ("OBSERVER_SSM_PREFIX", "COLLECTOR_KEY_SSM", "COLLECTOR_KEY_SSM_REGION"):
        assert key in text
    assert not any(" #" in v for v in values.values())  # no inline comments: they'd become part of the value


# ----------------------------------------------------------------------------- host checks

CHRONY_OUT = """Reference ID    : A9FEA97B (169.254.169.123)
Stratum         : 4
System time     : 0.000012345 seconds {dir} of NTP time
Last offset     : +0.000003000 seconds
"""


@pytest.mark.parametrize(("direction", "expected"), [("fast", 0.012345), ("slow", -0.012345)])
def test_parse_chrony(direction, expected):
    assert observer.parse_chrony_tracking(CHRONY_OUT.format(dir=direction)) == pytest.approx(expected)


def test_parse_chrony_garbage():
    assert observer.parse_chrony_tracking("506 Cannot talk to daemon") is None


def test_chrony_offset(monkeypatch):
    monkeypatch.setattr(observer.shutil, "which", lambda name: None)
    assert observer.chrony_offset_ms() is None
    monkeypatch.setattr(observer.shutil, "which", lambda name: "/usr/bin/chronyc")
    out = SimpleNamespace(stdout=CHRONY_OUT.format(dir="slow"))
    assert observer.chrony_offset_ms(run=lambda *a, **k: out) == pytest.approx(-0.012345)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("chronyc", 5)

    assert observer.chrony_offset_ms(run=boom) is None


def test_is_wsl():
    assert observer.is_wsl("Linux version 6.6.87.2-microsoft-standard-WSL2")
    assert not observer.is_wsl("Linux version 6.6.31+rpt-rpi-v8 (Raspberry Pi)")
    assert isinstance(observer._read_proc_version(), str)


def _check(env, offset=1.5, proc="Linux raspberrypi"):
    return observer.check(env, proc_version=lambda: proc, clock_offset=lambda: offset)


def test_check_ok():
    ok, lines = _check(BASE_ENV)
    assert ok
    assert lines[0].startswith("config OK: observer=pi-home")
    assert "  TG_SESSION from env" in lines
    assert lines[-1] == "clock OK: offset +1.5 ms"
    assert not any("1SESSION" in line or "ck" == line for line in lines)  # never prints secret values


@pytest.mark.parametrize(
    ("env", "offset", "proc", "needle"),
    [
        ({}, 1.0, "Linux", "config ERROR"),
        (BASE_ENV, 1.0, "Linux 6.6-microsoft-standard-WSL2", "WSL detected"),
        (BASE_ENV, -35.0, "Linux", "clock ERROR: offset -35.0 ms"),
    ],
)
def test_check_failures(env, offset, proc, needle):
    ok, lines = _check(env, offset, proc)
    assert not ok and any(needle in line for line in lines)


def test_check_without_chrony_is_a_warning_only():
    ok, lines = _check(BASE_ENV, offset=None)
    assert ok and any("clock WARNING" in line for line in lines)


# ----------------------------------------------------------------------------- CLI


def test_main_check(monkeypatch, capsys):
    monkeypatch.setattr(observer, "chrony_offset_ms", lambda: 0.4)
    monkeypatch.setattr(observer, "_read_proc_version", lambda: "Linux")
    assert observer.main(["check"], env=BASE_ENV) == 0
    assert "config OK" in capsys.readouterr().out
    assert observer.main(["check"], env={}) == 1


def test_main_env_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(observer, "chrony_offset_ms", lambda: 0.4)
    monkeypatch.setattr(observer, "_read_proc_version", lambda: "Linux")
    f = tmp_path / "observer.env"
    f.write_text("\n".join(f"{k}={v}" for k, v in BASE_ENV.items()))
    # Real environment variables win over the file; empty ones don't.
    assert observer.main(["--env-file", str(f), "check"], env={"OBSERVER_NAME": "override", "TG_API_ID": ""}) == 0
    assert "observer=override" in capsys.readouterr().out


def test_main_config_errors_exit_78(tmp_path, capsys):
    assert observer.main(["run"], env={}) == observer.EX_CONFIG
    assert "missing settings" in capsys.readouterr().err
    assert observer.main(["--env-file", str(tmp_path / "nope.env"), "run"], env={}) == observer.EX_CONFIG
    assert observer.main([], env={"TG_API_ID": "1"}) == observer.EX_CONFIG  # default command is run
