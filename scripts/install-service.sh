#!/usr/bin/env bash
#
# Install the health monitor as a systemd service.
#
# Idempotent: safe to run again after pulling changes. Re-running is in fact
# the normal way to deploy an update to the unit file or dependencies.
#
# Run as the normal user (pi), NOT with sudo. The script uses sudo only for
# the few steps that need it, so the venv ends up owned by you rather than
# root — a root-owned venv in your home directory is an annoyance you will
# have to fix later.
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

# The unit file contains absolute paths. If the repo is somewhere else, the
# service will fail at start with a confusing "no such file" — better to say
# so now than to let systemd say it later in a less obvious way.
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

# --system-site-packages is not optional. Without it the venv is sealed off
# from apt's packages and pip would install a second, competing copy of
# anything the system already provides.
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
# Find out here, where the error is on your screen, rather than after systemd
# has swallowed it into the journal.
say "Smoke test: one sample in the foreground"
"${VENV_DIR}/bin/python" "${REPO_DIR}/src/health_monitor.py" --once

# --- Make the journal survive a reboot -----------------------------------
#
# journald defaults to Storage=auto, which only writes to disk if
# /var/log/journal already exists. On a stock image it does not, so the journal
# lives in /run and is erased on every boot - including the reboot caused BY
# the brownout you are trying to investigate. A watchdog whose logs evaporate
# with the event is not a watchdog.
say "Checking that the journal is persistent"
if [[ -d /var/log/journal ]]; then
    echo "/var/log/journal exists - journald is already persistent."
else
    echo "journald is currently storing logs in /run, which is wiped on reboot."
    echo "Creating /var/log/journal so they survive a power cycle."
    sudo mkdir -p /var/log/journal
    sudo systemd-tmpfiles --create --prefix /var/log/journal
    sudo systemctl restart systemd-journald
    echo "Done. Note this is a system-wide change: ALL services now keep logs"
    echo "across reboots, and /var/log will grow. Cap it with SystemMaxUse= in"
    echo "/etc/systemd/journald.conf if space becomes a concern."
fi

# --- Install the unit ----------------------------------------------------
say "Installing ${UNIT_NAME}"
sudo cp "${UNIT_SRC}" "${UNIT_DEST}"
sudo systemctl daemon-reload

# start and enable are DIFFERENT THINGS, and this trips people up every year:
#
#   systemctl start <unit>    runs it right now. Gone after a reboot.
#   systemctl enable <unit>   makes it start at boot. Does not start it now.
#   systemctl enable --now    both, which is what we want.
#
# A service that was started but never enabled works perfectly all through
# testing and then silently does not come back after the first power cycle.
# That power cycle is usually at a competition. Always use enable --now.
say "Enabling and starting (enable --now = start now AND start at boot)"
sudo systemctl enable --now "${UNIT_NAME}"

# restart rather than start, so re-running this script picks up a changed
# unit file or new dependencies instead of quietly leaving the old one going.
sudo systemctl restart "${UNIT_NAME}"

say "Status"
sudo systemctl status "${UNIT_NAME}" --no-pager || true

cat <<'DONE'

Installed.

  Live logs:      journalctl -u health-monitor -f
  Since boot:     journalctl -u health-monitor -b
  Is it running:  systemctl status health-monitor
  One-off check:  python3 src/health_monitor.py --once

If the service is not running, read the status output above first. The most
common causes are a wrong path in the unit file and a missing video group
membership for the pi user (needed by vcgencmd).
DONE
