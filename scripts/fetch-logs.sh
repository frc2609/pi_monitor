#!/usr/bin/env bash
#
# Pull health logs off the Pi. Run this from your LAPTOP, not the Pi.
#
# Nothing is deleted on the Pi and nothing is installed there - it only needs
# to be reachable over SSH. Safe to run as often as you like.
#
# Usage:
#   ./scripts/fetch-logs.sh admin@2609-pi-1.local
#   ./scripts/fetch-logs.sh admin@10.26.9.11 ~/robot-logs
#   PI_HOST=admin@2609-pi-1.local ./scripts/fetch-logs.sh
#
# Run it after any session where something went wrong, and before reimaging.
# Reimaging is what actually loses history.

set -euo pipefail

TARGET="${1:-${PI_HOST:-}}"
DEST_ROOT="${2:-./fetched-logs}"

if [[ -z "${TARGET}" ]]; then
    echo "Usage: $0 <user@host> [destination]" >&2
    echo "   or: PI_HOST=<user@host> $0" >&2
    echo >&2
    echo "Example: $0 admin@2609-pi-1.local" >&2
    exit 1
fi

HOST_LABEL="${TARGET#*@}"
DEST="${DEST_ROOT}/${HOST_LABEL}"

say() { printf '\n==> %s\n' "$1"; }

say "Checking ${TARGET} is reachable"
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "${TARGET}" true 2>/dev/null; then
    # BatchMode fails if you use a password rather than a key, so try once more
    # interactively before giving up.
    if ! ssh -o ConnectTimeout=10 "${TARGET}" true; then
        echo "Could not reach ${TARGET}." >&2
        echo "Check the Pi is powered, on the network, and that the hostname resolves." >&2
        exit 1
    fi
fi

mkdir -p "${DEST}"

# --- the records ---------------------------------------------------------
#
# tar over ssh rather than rsync: Git Bash on Windows ships ssh and tar but
# not rsync, and the whole team needs to be able to run this.
say "Pulling health records"
if ssh "${TARGET}" "test -d /var/log/health-monitor && ls /var/log/health-monitor/*.jsonl >/dev/null 2>&1"; then
    ssh "${TARGET}" "tar czf - -C /var/log health-monitor" | tar xzf - -C "${DEST}"
    echo "  -> ${DEST}/health-monitor/"
else
    echo "  no .jsonl files on the Pi yet (log_dir may be null, or it has never run)"
fi

# --- boot state ----------------------------------------------------------
#
# Small, but it is the boot and brownout counters. Worth having next to the
# records they refer to.
say "Pulling boot state"
if ssh "${TARGET}" "test -f /var/lib/health-monitor/boot_state.json"; then
    ssh "${TARGET}" "cat /var/lib/health-monitor/boot_state.json" > "${DEST}/boot_state.json"
    echo "  -> ${DEST}/boot_state.json"
    cat "${DEST}/boot_state.json"
    echo
else
    echo "  no boot_state.json yet"
fi

# --- journal -------------------------------------------------------------
#
# This boot and the previous one. The previous boot is the interesting one
# after a power cut, since that is the boot that died.
say "Pulling journal (this boot and the one before)"
ssh "${TARGET}" "journalctl -u health-monitor -b 2>/dev/null || true" \
    > "${DEST}/journal-this-boot.txt"
ssh "${TARGET}" "journalctl -u health-monitor -b -1 2>/dev/null || true" \
    > "${DEST}/journal-previous-boot.txt"
echo "  -> ${DEST}/journal-this-boot.txt"
echo "  -> ${DEST}/journal-previous-boot.txt"

# --- summary -------------------------------------------------------------
say "Done"
echo "Everything is under ${DEST}"
echo
if [[ -d "${DEST}/health-monitor" ]]; then
    echo "Records by boot:"
    for f in "${DEST}"/health-monitor/*.jsonl; do
        [[ -e "${f}" ]] || continue
        printf '  %-40s %s lines\n' "$(basename "${f}")" "$(wc -l < "${f}" | tr -d ' ')"
    done
    echo
fi
echo "Faults across everything pulled:"
grep -h '"ok": false' "${DEST}"/health-monitor/*.jsonl 2>/dev/null | wc -l | xargs echo "  samples with ok=false:"
grep -lh 'BROWNOUT' "${DEST}"/journal-*.txt 2>/dev/null | while read -r f; do
    echo "  BROWNOUT reported in $(basename "${f}")"
done
echo
echo "Nothing was deleted on the Pi."
