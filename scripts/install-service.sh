#!/usr/bin/env bash
#
# Install the health monitor as a systemd service.
#
# Safe to re-run - that is the normal way to deploy a changed unit file.
# Run as pi, NOT with sudo: it sudos only the steps that need it, so the venv
# ends up owned by you instead of root.
#
# Usage:  ./scripts/install-service.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="health-monitor.service"
UNIT_SRC="${REPO_DIR}/services/${UNIT_NAME}"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"
VENV_DIR="${REPO_DIR}/.venv"
EXPECTED_DIR="/home/pi/vision/monitor"

say() { printf '\n==> %s\n' "$1"; }

if [[ ${EUID} -eq 0 ]]; then
    echo "Run this as your normal user, not with sudo." >&2
    echo "It will ask for sudo on the steps that actually need it." >&2
    exit 1
fi

# The unit file has absolute paths in it. Cloned elsewhere, the service fails
# at start with a confusing "no such file", so say so now instead.
if [[ "${REPO_DIR}" != "${EXPECTED_DIR}" ]]; then
    echo "WARNING: repo is at ${REPO_DIR}, but ${UNIT_NAME} has ${EXPECTED_DIR}" >&2
    echo "         hardcoded in WorkingDirectory and ExecStart." >&2
    echo "         Either clone to ${EXPECTED_DIR}, or edit those paths in" >&2
    echo "         services/${UNIT_NAME} before continuing." >&2
    read -r -p "Continue anyway? [y/N] " reply
    [[ "${reply}" =~ ^[Yy]$ ]] || exit 1
fi

# --- Dependencies --------------------------------------------------------
#
# PyYAML comes from apt rather than pip. See requirements.txt for why.
say "Checking for python3-yaml (apt)"
if dpkg -s python3-yaml >/dev/null 2>&1; then
    echo "python3-yaml already installed."
else
    echo "Installing python3-yaml..."
    sudo apt-get update
    sudo apt-get install -y python3-yaml
fi

# --system-site-packages is not optional: without it the venv can't see apt's
# packages, and pip installs a second competing copy of OpenCV.
say "Setting up the virtual environment"
if [[ -d "${VENV_DIR}" ]]; then
    echo "venv already exists at ${VENV_DIR}"
else
    echo "Creating venv at ${VENV_DIR}"
    python3 -m venv --system-site-packages "${VENV_DIR}"
fi

echo "Installing pip dependencies (pyntcore)..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"

# --- Smoke test before installing ----------------------------------------
#
# Fail here, where you can see it, not inside systemd.
say "Smoke test: one sample in the foreground"
"${VENV_DIR}/bin/python" "${REPO_DIR}/src/health_monitor.py" --once

# --- Make the journal survive a reboot -----------------------------------
#
# journald only writes logs to disk if /var/log/journal exists, and on a stock
# image it doesn't - so logs live in /run and are erased on every boot,
# including the reboot caused by the brownout you're investigating.
say "Checking that the journal is persistent"
if [[ -d /var/log/journal ]]; then
    echo "/var/log/journal exists - journald is already persistent."
else
    echo "journald is currently storing logs in /run, which is wiped on reboot."
    echo "Creating /var/log/journal so they survive a power cycle."
    sudo mkdir -p /var/log/journal
    sudo systemd-tmpfiles --create --prefix /var/log/journal
    sudo systemctl restart systemd-journald
    echo "Note: system-wide change. All services now keep logs across reboots."
    echo "Cap the size with SystemMaxUse= in /etc/systemd/journald.conf."
fi

# --- Install the unit ----------------------------------------------------
say "Installing ${UNIT_NAME}"
sudo cp "${UNIT_SRC}" "${UNIT_DEST}"
sudo systemctl daemon-reload

# start and enable are different things, and this catches people every year:
#   start        runs it now, gone after a reboot
#   enable       starts it at boot, doesn't start it now
#   enable --now both
# Started-but-never-enabled works all through testing, then silently doesn't
# come back after the first power cycle - usually at a competition.
say "Enabling and starting (enable --now = start now AND start at boot)"
sudo systemctl enable --now "${UNIT_NAME}"

# restart, so re-running this picks up a changed unit file.
sudo systemctl restart "${UNIT_NAME}"

say "Status"
sudo systemctl status "${UNIT_NAME}" --no-pager || true

cat <<'DONE'

Installed.

  Live logs:      journalctl -u health-monitor -f
  Since boot:     journalctl -u health-monitor -b
  Is it running:  systemctl status health-monitor
  One-off check:  python3 src/health_monitor.py --once

If it isn't running, read the status output above. Usually a wrong path in the
unit file, or the pi user missing from the video group (vcgencmd needs it).
DONE
