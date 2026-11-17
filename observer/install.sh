#!/usr/bin/env bash
# Install or update the tv-tg-bot observer as a systemd service on any Linux host:
# Raspberry Pi OS / Debian / Ubuntu (apt) or Amazon Linux 2023 / Fedora / RHEL (dnf).
# The EC2 option (infra observer_host = "ec2") runs this same script from its cloud-init user data.
#
#   sudo ./install.sh                         # first install: creates /etc/tvtg-observer.env to fill in
#   sudo ./install.sh                         # again after editing it (or after updating observer.py)
#   sudo ./install.sh --env-file my.env       # install a prepared env file
#   ./install.sh --dry-run                    # print what would happen
#
# Re-running is safe: it updates the code, venv and unit in place and restarts the service.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [options]
  --src DIR        directory holding observer.py etc. (default: this script's directory)
  --dir DIR        install directory (default: /opt/tvtg-observer)
  --user NAME      service account (default: tvtg)
  --env-file FILE  install FILE as /etc/tvtg-observer.env (default: keep existing, or create from the example)
  --with-aws       also install boto3, so secrets can come from SSM
  --pkg apt|dnf    package manager (default: auto-detect)
  --no-start       install and enable, but don't (re)start the service
  --print-unit     print the rendered systemd unit and exit
  --dry-run        print the commands instead of running them
EOF
}

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR=/opt/tvtg-observer
SVC_USER=tvtg
ENV_FILE=/etc/tvtg-observer.env
UNIT_FILE=/etc/systemd/system/tvtg-observer.service
ENV_SRC=""
WITH_AWS=0
PKG=""
START=1
PRINT_UNIT=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src) SRC="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --user) SVC_USER="$2"; shift 2 ;;
    --env-file) ENV_SRC="$2"; shift 2 ;;
    --with-aws) WITH_AWS=1; shift ;;
    --pkg) PKG="$2"; shift 2 ;;
    --no-start) START=0; shift ;;
    --print-unit) PRINT_UNIT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "install.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

render_unit() {
  sed -e "s|@DIR@|${DIR}|g" -e "s|@USER@|${SVC_USER}|g" "${SRC}/tvtg-observer.service"
}

if ((PRINT_UNIT)); then
  render_unit
  exit 0
fi

run() {
  if ((DRY_RUN)); then printf '+ %s\n' "$*"; else "$@"; fi
}

log() { printf '==> %s\n' "$*"; }

for f in observer.py requirements.txt requirements-aws.txt tvtg-observer.service observer.env.example; do
  [[ -f "${SRC}/${f}" ]] || { echo "install.sh: ${SRC}/${f} not found (use --src)" >&2; exit 1; }
done
if ((!DRY_RUN)) && [[ ${EUID} -ne 0 ]]; then
  echo "install.sh: run as root (sudo), or use --dry-run" >&2
  exit 1
fi
if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "install.sh: WARNING: this looks like WSL, whose clock drifts after the host sleeps. Use another machine." >&2
fi

# ----------------------------------------------------------------------------- packages + clock sync
if [[ -z "${PKG}" ]]; then
  if command -v apt-get >/dev/null 2>&1; then PKG=apt
  elif command -v dnf >/dev/null 2>&1; then PKG=dnf
  else
    echo "install.sh: need apt-get or dnf. On other systems use Docker (observer/compose.yaml)." >&2
    exit 1
  fi
fi

log "packages (${PKG})"
case "${PKG}" in
  apt)
    run apt-get update -q
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 python3-venv chrony
    CHRONY_SERVICE=chrony
    ;;
  dnf)
    run dnf install -y -q python3 chrony
    run dnf install -y -q python3.11 || true # newer interpreter where available (Amazon Linux 2023)
    CHRONY_SERVICE=chronyd
    ;;
  *) echo "install.sh: --pkg must be apt or dnf" >&2; exit 2 ;;
esac
run systemctl enable --now "${CHRONY_SERVICE}"

PY=""
for candidate in python3.12 python3.11 python3; do
  if command -v "${candidate}" >/dev/null 2>&1; then PY="$(command -v "${candidate}")"; break; fi
done
if [[ -z "${PY}" ]]; then
  if ((DRY_RUN)); then PY=python3; else echo "install.sh: no python3 found" >&2; exit 1; fi
fi
if ((!DRY_RUN)) && ! "${PY}" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  echo "install.sh: ${PY} is older than 3.9" >&2
  exit 1
fi

# ----------------------------------------------------------------------------- user, code, venv
log "service user ${SVC_USER}, code in ${DIR} (python: ${PY})"
if ! id -u "${SVC_USER}" >/dev/null 2>&1; then
  run useradd --system --user-group --home-dir "${DIR}" --shell /usr/sbin/nologin "${SVC_USER}"
fi
run install -d -m 750 "${DIR}"
run install -m 644 "${SRC}/observer.py" "${SRC}/requirements.txt" "${SRC}/requirements-aws.txt" "${DIR}/"
[[ -x "${DIR}/.venv/bin/python" ]] || run "${PY}" -m venv "${DIR}/.venv"
run "${DIR}/.venv/bin/pip" install -q --upgrade pip
run "${DIR}/.venv/bin/pip" install -q -r "${DIR}/requirements.txt"
if ((WITH_AWS)); then
  run "${DIR}/.venv/bin/pip" install -q -r "${DIR}/requirements-aws.txt"
fi
run chown -R "${SVC_USER}:${SVC_USER}" "${DIR}"

# ----------------------------------------------------------------------------- config
NEEDS_CONFIG=0
if [[ -n "${ENV_SRC}" ]]; then
  if [[ "$(realpath -m "${ENV_SRC}")" != "${ENV_FILE}" ]]; then
    run install -m 600 -o root -g root "${ENV_SRC}" "${ENV_FILE}"
  fi
elif [[ ! -e "${ENV_FILE}" ]]; then
  run install -m 600 -o root -g root "${SRC}/observer.env.example" "${ENV_FILE}"
  NEEDS_CONFIG=1
fi

# ----------------------------------------------------------------------------- systemd
log "systemd unit ${UNIT_FILE}"
if ((DRY_RUN)); then
  printf '+ write %s\n' "${UNIT_FILE}"
else
  render_unit >"${UNIT_FILE}.tmp"
  chmod 644 "${UNIT_FILE}.tmp"
  mv "${UNIT_FILE}.tmp" "${UNIT_FILE}"
fi
run systemctl daemon-reload
run systemctl enable tvtg-observer

if ((NEEDS_CONFIG)); then
  cat <<EOF

Next:
  1. fill in ${ENV_FILE} (TG_API_ID and TG_API_HASH first; see the comments in it)
  2. sudo ${DIR}/.venv/bin/python ${DIR}/observer.py --env-file ${ENV_FILE} login
     and put the printed TG_SESSION into ${ENV_FILE}
  3. run this script again
EOF
  exit 0
fi

if ((START)); then
  run systemctl restart tvtg-observer
fi

log "self-check"
if ((DRY_RUN)); then
  printf '+ %s\n' "${DIR}/.venv/bin/python ${DIR}/observer.py --env-file ${ENV_FILE} check"
else
  "${DIR}/.venv/bin/python" "${DIR}/observer.py" --env-file "${ENV_FILE}" check || true
fi
log "done. Logs: journalctl -u tvtg-observer -f"
