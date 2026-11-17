"""Reception observer: your own Telegram account, logged in via MTProto (Telethon).

It records the moment each alert-channel message reaches this client (t9, local clock, ms) and reports it to
the collector Lambda. The program is the same on every host type (docs/deploy.md step 8):

  * any Linux machine (Raspberry Pi, mini PC, VPS)  -> observer/install.sh (systemd)
  * EC2 t4g.nano managed by Terraform               -> infra observer_host = "ec2" (secrets come from SSM)
  * anything that runs Docker natively              -> observer/compose.yaml

Commands:
  observer.py run      observe and report (default; what the service runs)
  observer.py login    interactive one-time login; prints a TG_SESSION string (or stores it in SSM with --ssm)
  observer.py check    validate configuration and clock sync without connecting to Telegram

Configuration (environment variables; secrets may instead come from SSM, see observer.env.example):
  CHAT_IDS            comma-separated channel ids to watch (-100...)
  COLLECTOR_URL       collector Function URL
  COLLECTOR_KEY       shared HMAC key                  | or COLLECTOR_KEY_SSM (+ COLLECTOR_KEY_SSM_REGION)
  TG_API_ID/HASH      from https://my.telegram.org     | or OBSERVER_SSM_PREFIX/api_id, /api_hash
  TG_SESSION          output of `observer.py login`    | or OBSERVER_SSM_PREFIX/session, or a SESSION_PATH file
  OBSERVER_NAME       label for this host in the dashboard (default: hostname)

Kept compatible with Python 3.9 (Amazon Linux 2023's system python) and independent of the Lambda package.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

REPORT_DELAY_S = 2.0  # give the sender's DynamoDB PutItem time to land
RETRY_DELAY_S = 3.0
RETRIES = 3
MAX_CLOCK_OFFSET_MS = 20.0
PLACEHOLDER = "CHANGE_ME"
EX_CONFIG = 78  # sysexits.h; systemd restarts the service, so it recovers once the config is fixed

SSM_SECRETS = (("TG_API_ID", "api_id"), ("TG_API_HASH", "api_hash"), ("TG_SESSION", "session"))
SSM_GET = Callable[[str, Optional[str]], str]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    chat_ids: list = field(default_factory=list)
    collector_url: str = ""
    collector_key: bytes = b""
    session: str | None = None  # Telethon StringSession
    session_path: str = "observer.session"
    name: str = ""
    sources: dict = field(default_factory=dict)  # setting -> "env" | "ssm:<name>" (never the value)


# ----------------------------------------------------------------------------- reporting


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def build_report(chat_id: int, message_id: int, recv_ms: int, observer: str | None = None) -> bytes:
    report: dict[str, Any] = {"chat_id": int(chat_id), "message_id": int(message_id), "recv_ms": int(recv_ms)}
    if observer:
        report["observer"] = str(observer)[:64]
    return json.dumps(report, separators=(",", ":")).encode()


def sign(key: bytes, body: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def post_report(
    url: str,
    key: bytes,
    body: bytes,
    *,
    retries: int = RETRIES,
    retry_delay_s: float = RETRY_DELAY_S,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """POST a signed report. Retries on 404 (not stored yet), 5xx and network errors. Returns the final status."""
    headers = {"Content-Type": "application/json", "X-Sig": sign(key, body)}
    status = 0
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=5) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status != 404 and status < 500:
                return status
        except (urllib.error.URLError, OSError):
            status = 0
        if attempt < retries - 1:
            sleep(retry_delay_s)
    return status


def make_handler(
    cfg: Config,
    *,
    post: Callable[..., int] = post_report,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], int] = now_ms,
    out: Callable[[str], Any] = print,
) -> Callable[[Any], Awaitable[int]]:
    """The Telethon NewMessage callback: stamp t9 first, wait for the sender's PutItem, then report."""

    async def on_message(event: Any) -> int:
        t9 = clock()
        body = build_report(event.chat_id, event.message.id, t9, cfg.name)
        await sleep(REPORT_DELAY_S)
        status = await asyncio.to_thread(post, cfg.collector_url, cfg.collector_key, body)
        out(json.dumps({"chat_id": event.chat_id, "message_id": event.message.id, "t9": t9, "status": status}))
        return status

    return on_message


# ----------------------------------------------------------------------------- configuration


def _usable(value: str | None) -> bool:
    return bool(value) and value.strip() != PLACEHOLDER


def _ssm_get_boto3(name: str, region: str | None) -> str:  # pragma: no cover - exercised via moto in tests
    import boto3  # only needed when secrets come from SSM (requirements-aws.txt)

    ssm = boto3.client("ssm", region_name=region)
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def resolve(env: Mapping[str, str], ssm_get: SSM_GET | None = None) -> tuple[dict, dict]:
    """Merge environment values with SSM-backed secrets. Environment wins; placeholders count as unset."""
    values = dict(env)
    sources = {k: "env" for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION", "COLLECTOR_KEY") if _usable(env.get(k))}
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or None
    wanted = []
    prefix = (env.get("OBSERVER_SSM_PREFIX") or "").rstrip("/")
    if prefix:
        wanted += [(key, f"{prefix}/{suffix}", region) for key, suffix in SSM_SECRETS]
    if env.get("COLLECTOR_KEY_SSM"):
        wanted.append(("COLLECTOR_KEY", env["COLLECTOR_KEY_SSM"], env.get("COLLECTOR_KEY_SSM_REGION") or region))
    getter = ssm_get or _ssm_get_boto3
    for key, name, reg in wanted:
        if key in sources:
            continue
        try:
            value = getter(name, reg)
        except Exception as exc:  # noqa: BLE001 - missing parameter, no credentials, no boto3...
            raise ConfigError(f"cannot read SSM parameter {name}: {exc}") from exc
        if _usable(value):
            values[key] = value
            sources[key] = f"ssm:{name}"
    return values, sources


def load_config(env: Mapping[str, str], ssm_get: SSM_GET | None = None, purpose: str = "run") -> Config:
    values, sources = resolve(env, ssm_get)
    required = ["TG_API_ID", "TG_API_HASH"]
    if purpose == "run":
        required += ["CHAT_IDS", "COLLECTOR_URL", "COLLECTOR_KEY"]
    missing = [k for k in required if not _usable(values.get(k))]
    if missing:
        hint = " (SSM parameters may still hold CHANGE_ME)" if env.get("OBSERVER_SSM_PREFIX") else ""
        raise ConfigError(f"missing settings: {', '.join(missing)}{hint}")
    try:
        api_id = int(values["TG_API_ID"])
        chat_ids = [int(c) for c in values.get("CHAT_IDS", "").split(",") if c.strip()]
    except ValueError as exc:
        raise ConfigError(f"TG_API_ID and CHAT_IDS must be integers: {exc}") from exc
    if purpose == "run" and not chat_ids:
        raise ConfigError("CHAT_IDS is empty")

    session = values.get("TG_SESSION") if _usable(values.get("TG_SESSION")) else None
    session_path = values.get("SESSION_PATH") or "observer.session"
    if purpose == "run" and session is None:
        file = session_path if session_path.endswith(".session") else session_path + ".session"
        if not os.path.exists(file):
            raise ConfigError("no Telegram session: set TG_SESSION (run `observer.py login`) or SESSION_PATH")
        sources["session"] = f"file:{file}"

    return Config(
        api_id=api_id,
        api_hash=values["TG_API_HASH"],
        chat_ids=chat_ids,
        collector_url=values.get("COLLECTOR_URL", ""),
        collector_key=values.get("COLLECTOR_KEY", "").encode(),
        session=session,
        session_path=session_path,
        name=(values.get("OBSERVER_NAME") or socket.gethostname())[:64],
        sources=sources,
    )


# ----------------------------------------------------------------------------- host checks

_CHRONY_RE = re.compile(r"^System time\s*:\s*([0-9.]+) seconds (fast|slow)", re.MULTILINE)


def parse_chrony_tracking(text: str) -> float | None:
    """Offset in ms from `chronyc tracking` (positive = local clock is fast)."""
    m = _CHRONY_RE.search(text)
    if not m:
        return None
    ms = float(m.group(1)) * 1000
    return ms if m.group(2) == "fast" else -ms


def chrony_offset_ms(run: Callable[..., Any] = subprocess.run) -> float | None:
    if shutil.which("chronyc") is None:
        return None
    try:
        out = run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_chrony_tracking(out.stdout or "")


def is_wsl(proc_version: str) -> bool:
    return "microsoft" in proc_version.lower()


def _read_proc_version() -> str:
    try:
        with open("/proc/version") as fh:
            return fh.read()
    except OSError:
        return ""


def check(
    env: Mapping[str, str],
    ssm_get: SSM_GET | None = None,
    proc_version: Callable[[], str] | None = None,
    clock_offset: Callable[[], float | None] | None = None,
) -> tuple[bool, list]:
    proc_version = proc_version or _read_proc_version
    clock_offset = clock_offset or chrony_offset_ms
    ok, lines = True, []
    try:
        cfg = load_config(env, ssm_get, "run")
        lines.append(f"config OK: observer={cfg.name} chats={cfg.chat_ids} collector={cfg.collector_url}")
        lines += [f"  {key} from {src}" for key, src in sorted(cfg.sources.items())]
    except ConfigError as exc:
        ok = False
        lines.append(f"config ERROR: {exc}")
    if is_wsl(proc_version()):
        ok = False
        lines.append("host ERROR: WSL detected; its clock drifts after the host sleeps. Use another machine.")
    offset = clock_offset()
    if offset is None:
        lines.append("clock WARNING: chronyc not available, sync not verified (in Docker, check the host)")
    elif abs(offset) > MAX_CLOCK_OFFSET_MS:
        ok = False
        lines.append(f"clock ERROR: offset {offset:+.1f} ms exceeds {MAX_CLOCK_OFFSET_MS:.0f} ms")
    else:
        lines.append(f"clock OK: offset {offset:+.1f} ms")
    return ok, lines


# ----------------------------------------------------------------------------- Telethon glue


def _session(cfg: Config) -> Any:
    if cfg.session:
        from telethon.sessions import StringSession

        return StringSession(cfg.session)
    return cfg.session_path


def run(cfg: Config) -> None:  # pragma: no cover - needs a live Telegram login
    from telethon import TelegramClient, events

    client = TelegramClient(_session(cfg), cfg.api_id, cfg.api_hash)
    client.add_event_handler(make_handler(cfg), events.NewMessage(chats=cfg.chat_ids))
    print(f"observer {cfg.name}: watching {cfg.chat_ids}", flush=True)
    client.start()  # never prompts with a valid session
    client.run_until_disconnected()


def login(cfg: Config, env: Mapping[str, str], to_ssm: bool) -> None:  # pragma: no cover - interactive
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    with TelegramClient(StringSession(), cfg.api_id, cfg.api_hash) as client:
        session = client.session.save()
    if to_ssm:
        import boto3

        prefix = env["OBSERVER_SSM_PREFIX"].rstrip("/")
        boto3.client("ssm", region_name=env.get("AWS_REGION")).put_parameter(
            Name=f"{prefix}/session", Value=session, Type="SecureString", Overwrite=True)
        print(f"stored in SSM {prefix}/session; the service picks it up on its next restart (within 30 s)")
    else:
        print("\nTG_SESSION (treat it like a password; use a separate login per observer host):\n")
        print(session)


def read_env_file(path: str) -> dict:
    """KEY=value lines, as systemd's EnvironmentFile and Docker's env_file read them (no inline comments)."""
    values = {}
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def main(argv: list | None = None, env: Mapping[str, str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="tv-tg-bot Telegram reception observer")
    ap.add_argument("--env-file", help="read settings from this file too (real environment variables win)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="observe and report (default)")
    p_login = sub.add_parser("login", help="interactive login; prints TG_SESSION")
    p_login.add_argument("--ssm", action="store_true", help="store the session in OBSERVER_SSM_PREFIX/session")
    sub.add_parser("check", help="validate configuration and clock sync")
    args = ap.parse_args(argv)
    env = dict(os.environ if env is None else env)
    if args.env_file:
        try:
            env = {**read_env_file(args.env_file), **{k: v for k, v in env.items() if v}}
        except OSError as exc:
            print(f"observer: {exc}", file=sys.stderr)
            return EX_CONFIG
    cmd = args.cmd or "run"

    if cmd == "check":
        ok, lines = check(env)
        print("\n".join(lines))
        return 0 if ok else 1
    try:
        cfg = load_config(env, purpose="login" if cmd == "login" else "run")
    except ConfigError as exc:
        print(f"observer: {exc}", file=sys.stderr)
        return EX_CONFIG
    if cmd == "login":  # pragma: no cover
        if args.ssm and not env.get("OBSERVER_SSM_PREFIX"):
            print("observer: --ssm needs OBSERVER_SSM_PREFIX", file=sys.stderr)
            return EX_CONFIG
        login(cfg, env, args.ssm)
        return 0
    run(cfg)  # pragma: no cover
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
