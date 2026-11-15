"""Host-provisioning artifacts for the observer: install.sh, the systemd unit, Docker, and EC2 cloud-init."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "observer"
INSTALL = OBS / "install.sh"
TEMPLATE = ROOT / "infra/modules/observer_ec2/user_data.sh.tftpl"


def run_install(*args, check=True):
    res = subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True, timeout=30)
    if check:
        assert res.returncode == 0, res.stderr
    return res


def commands(out: str) -> list[str]:
    return [line[2:] for line in out.splitlines() if line.startswith("+ ")]


def shellcheck() -> str:
    exe = shutil.which("shellcheck") or str(ROOT / ".venv/bin/shellcheck")
    if not Path(exe).exists():
        pytest.skip("shellcheck not installed (pip install shellcheck-py)")
    return exe


# ----------------------------------------------------------------------------- install.sh


def test_install_script_passes_shellcheck():
    res = subprocess.run([shellcheck(), str(INSTALL)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_apt_dry_run():
    cmds = commands(run_install("--dry-run", "--pkg", "apt", "--dir", "/opt/obs", "--user", "obs").stdout)
    joined = "\n".join(cmds)
    assert "env DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 python3-venv chrony" in cmds
    assert "systemctl enable --now chrony" in cmds
    assert "dnf" not in joined
    assert "useradd --system --user-group --home-dir /opt/obs --shell /usr/sbin/nologin obs" in cmds
    assert any(c.endswith("-m venv /opt/obs/.venv") for c in cmds)
    assert "/opt/obs/.venv/bin/pip install -q -r /opt/obs/requirements.txt" in cmds
    assert "requirements-aws.txt" not in " ".join(c for c in cmds if "pip install" in c)
    assert "chown -R obs:obs /opt/obs" in cmds
    assert "write /etc/systemd/system/tvtg-observer.service" in cmds
    assert ["systemctl daemon-reload", "systemctl enable tvtg-observer"] == [
        c for c in cmds if c in ("systemctl daemon-reload", "systemctl enable tvtg-observer")]


def test_dnf_dry_run_with_aws():
    cmds = commands(run_install("--dry-run", "--pkg", "dnf", "--with-aws").stdout)
    assert "dnf install -y -q python3 chrony" in cmds and "dnf install -y -q python3.11" in cmds
    assert "systemctl enable --now chronyd" in cmds
    assert "/opt/tvtg-observer/.venv/bin/pip install -q -r /opt/tvtg-observer/requirements-aws.txt" in cmds


def test_first_install_creates_env_file_and_stops_for_configuration():
    res = run_install("--dry-run", "--pkg", "apt")
    cmds = commands(res.stdout)
    assert any(c.startswith("install -m 600 -o root -g root") and c.endswith("/etc/tvtg-observer.env") for c in cmds)
    assert "systemctl restart tvtg-observer" not in cmds  # nothing to run until the env file is filled in
    assert "observer.py --env-file /etc/tvtg-observer.env login" in res.stdout


def test_install_with_prepared_env_file_starts_and_checks(tmp_path):
    env = tmp_path / "my.env"
    env.write_text("TG_API_ID=1\n")
    cmds = commands(run_install("--dry-run", "--pkg", "apt", "--env-file", str(env)).stdout)
    assert f"install -m 600 -o root -g root {env} /etc/tvtg-observer.env" in cmds
    assert "systemctl restart tvtg-observer" in cmds
    assert any(c.endswith("observer.py --env-file /etc/tvtg-observer.env check") for c in cmds)


def test_env_file_already_in_place_is_not_copied_onto_itself():
    cmds = commands(run_install("--dry-run", "--pkg", "dnf", "--env-file", "/etc/tvtg-observer.env").stdout)
    assert not any(c.endswith(" /etc/tvtg-observer.env") and c.startswith("install") for c in cmds)
    assert "systemctl restart tvtg-observer" in cmds


def test_no_start():
    cmds = commands(run_install("--dry-run", "--pkg", "apt", "--env-file", "/etc/tvtg-observer.env",
                                "--no-start").stdout)
    assert "systemctl restart tvtg-observer" not in cmds and "systemctl enable tvtg-observer" in cmds


def test_rendered_unit():
    unit = run_install("--print-unit", "--dir", "/srv/obs", "--user", "watcher").stdout
    assert "@" not in unit.replace("network-online.target", "")
    assert "User=watcher" in unit and "Group=watcher" in unit
    assert "ExecStart=/srv/obs/.venv/bin/python /srv/obs/observer.py run" in unit
    assert "EnvironmentFile=-/etc/tvtg-observer.env" in unit
    assert "Restart=always" in unit and "RestartSec=30" in unit
    assert "ReadWritePaths=/srv/obs" in unit


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [(["--bogus"], 2, "unknown option"), (["--dry-run", "--pkg", "pacman"], 2, "--pkg must be apt or dnf"),
     (["--src", "/nonexistent"], 1, "not found")],
)
def test_install_errors(args, code, message):
    res = run_install(*args, check=False)
    assert res.returncode == code and message in res.stderr


def test_help():
    assert "Usage: install.sh" in run_install("--help").stdout


@pytest.mark.skipif(subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() == "0",
                    reason="running as root")
def test_refuses_to_run_without_root():
    res = run_install("--pkg", "apt", check=False)
    assert res.returncode == 1 and "run as root" in res.stderr


# ----------------------------------------------------------------------------- Docker


def test_dockerfile_and_compose_are_consistent():
    dockerfile = (OBS / "Dockerfile").read_text()
    compose = (OBS / "compose.yaml").read_text()
    ignore = (OBS / ".dockerignore").read_text().split()
    for copied in re.findall(r"^COPY (.+) \./$", dockerfile, re.MULTILINE):
        for name in copied.split():
            assert f"!{name}" in ignore and (OBS / name).exists()
    assert "USER tvtg" in dockerfile and 'ENTRYPOINT ["python", "observer.py"]' in dockerfile
    assert "env_file: observer.env" in compose and "restart: unless-stopped" in compose
    assert "observer/observer.env" in (ROOT / ".gitignore").read_text()


# ----------------------------------------------------------------------------- EC2 cloud-init


def render_user_data(template: str | None = None) -> str:
    """Render the Terraform template the way templatefile() does for plain ${var} interpolations."""
    template = TEMPLATE.read_text() if template is None else template
    assert "%{" not in template  # no directives, so plain substitution is faithful
    values = {
        "observer_py": (OBS / "observer.py").read_text(),
        "install_sh": INSTALL.read_text(),
        "unit": (OBS / "tvtg-observer.service").read_text(),
        "requirements": (OBS / "requirements.txt").read_text(),
        "requirements_aws": (OBS / "requirements-aws.txt").read_text(),
        "env_example": (OBS / "observer.env.example").read_text(),
        "observer_name": "ec2-us-east-2",
        "chat_ids": "-1001,-1002",
        "collector_url": "https://collector.example/",
        "ssm_prefix": "/tvtg-prod/observer",
        "region": "us-east-2",
        "collector_key_param": "/tvtg-prod/collector_key",
        "pipeline_region": "us-west-2",
    }
    rendered = re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], template)
    return rendered


def test_user_data_heredoc_markers_never_appear_in_embedded_files():
    markers = re.findall(r"<<'(TVTG_EOF_\w+)'", TEMPLATE.read_text())
    assert len(markers) == len(set(markers)) == 7
    for path in (OBS / "observer.py", INSTALL, OBS / "tvtg-observer.service", OBS / "requirements.txt",
                 OBS / "requirements-aws.txt", OBS / "observer.env.example"):
        assert not any(m in path.read_text() for m in markers), path


def test_rendered_user_data_is_valid_bash(tmp_path):
    script = tmp_path / "user_data.sh"
    script.write_text(render_user_data())
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    res = subprocess.run([shellcheck(), "-S", "warning", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_user_data_reproduces_the_files_exactly(tmp_path):
    """Run just the file-writing part of the cloud-init script and compare with the repo's files."""
    template = TEMPLATE.read_text()  # sandbox the template's own paths, not the embedded files' text
    template = template.replace("/opt/tvtg-observer-src", str(tmp_path / "src"))
    template = template.replace("cat > /etc/tvtg-observer.env", f"cat > {tmp_path / 'observer.env'}")
    template = template.replace("/var/log/tvtg-observer-bootstrap.log", str(tmp_path / "bootstrap.log"))
    rendered = render_user_data(template)
    head, _, _ = rendered.partition('bash "$SRC/install.sh"')
    script = tmp_path / "write_files.sh"
    script.write_text(head)
    subprocess.run(["bash", str(script)], check=True, capture_output=True, timeout=30)
    for name in ("observer.py", "install.sh", "tvtg-observer.service", "requirements.txt", "requirements-aws.txt",
                 "observer.env.example"):
        written = (tmp_path / "src" / name).read_text()
        original = (OBS / name).read_text()
        assert written.rstrip("\n") == original.rstrip("\n"), name
    env = (tmp_path / "observer.env").read_text()
    assert "OBSERVER_SSM_PREFIX=/tvtg-prod/observer" in env and "COLLECTOR_KEY_SSM_REGION=us-west-2" in env
    assert (tmp_path / "observer.env").stat().st_mode & 0o077 == 0  # umask 077: root-only
    assert 'bash "$SRC/install.sh" --src "$SRC" --env-file /etc/tvtg-observer.env --with-aws --pkg dnf' in \
        render_user_data()
