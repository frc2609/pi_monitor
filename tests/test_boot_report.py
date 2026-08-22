"""
Tests for what the monitor says after an unclean boot, and for the pre-alarm
flush trigger.

Runs anywhere - no Pi needed:

    python3 tests/test_boot_report.py
"""
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "src")

import health_monitor as hm
from health_monitor import (DEFAULTS, RESTART_LOOP_WARN, HealthMonitor,
                            deep_merge, read_last_records)


class Captured(logging.Handler):
    """Collect log lines so we can assert on what the monitor reported."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def text(self):
        return "\n".join(self.lines)


def monitor_with_log(tmp, volts):
    """A monitor whose previous boot left `volts` as its last records."""
    logs = Path(tmp) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with (logs / "health-b00001-20260822-101500.jsonl").open("w") as f:
        for i, v in enumerate(volts):
            f.write(json.dumps({
                "ts": now - (len(volts) - i), "volts_in": v,
                "temp_c": 55.0, "load1": 0.4, "ok": v >= 4.75,
            }) + "\n")
    cfg = deep_merge(DEFAULTS, {"health": {
        "nt_enabled": False,
        "log_dir": str(logs),
        "state_dir": str(Path(tmp) / "state"),
    }})
    return HealthMonitor(cfg, nt_enabled=False, read_only=True), logs


def report(mon, unclean=True):
    cap = Captured()
    log = logging.getLogger("health-monitor")
    log.addHandler(cap)
    try:
        mon.state.last_boot_was_unclean = unclean
        mon.report_previous_boot()
    finally:
        log.removeHandler(cap)
    return cap.text()


# Voltage falling into the cut -> brownout.
with tempfile.TemporaryDirectory() as tmp:
    mon, logs = monitor_with_log(tmp, [5.10, 5.02, 4.93, 4.81, 4.68])
    out = report(mon)
    assert "brownout" in out, out
    assert "4.68V" in out, out
    assert len(read_last_records(logs)) == 5
    print("PASS  falling voltage reads as a brownout")

# Healthy right up to the cut -> something disconnected it.
with tempfile.TemporaryDirectory() as tmp:
    mon, _ = monitor_with_log(tmp, [5.10, 5.09, 5.10, 5.09, 5.10])
    out = report(mon)
    assert "disconnected" in out, out
    assert "brownout rather" not in out, out
    print("PASS  steady voltage reads as power being disconnected")

# Unclean boot, but the log did not survive.
with tempfile.TemporaryDirectory() as tmp:
    cfg = deep_merge(DEFAULTS, {"health": {
        "nt_enabled": False, "log_dir": str(Path(tmp) / "gone"),
        "state_dir": str(Path(tmp) / "state")}})
    out = report(HealthMonitor(cfg, nt_enabled=False, read_only=True))
    assert "no log records survived" in out, out
    print("PASS  missing log is reported, not crashed on")

# A clean boot must say nothing at all.
with tempfile.TemporaryDirectory() as tmp:
    mon, _ = monitor_with_log(tmp, [5.10, 5.10])
    assert report(mon, unclean=False) == ""
    print("PASS  clean boot stays quiet")

# Pre-alarm: flush while the voltage is still falling, before it fails.
cfg = deep_merge(DEFAULTS, {"health": {"nt_enabled": False, "log_dir": None}})
mon = HealthMonitor(cfg, nt_enabled=False, read_only=True)
limit = cfg["health"]["thresholds"]["volts_prealarm"]
assert not mon.prealarm({"volts_in": limit + 0.10})
assert mon.prealarm({"volts_in": limit})
assert mon.prealarm({"volts_in": limit - 0.10})
assert not mon.prealarm({"volts_in": None})   # no reading is not an alarm
print("PASS  pre-alarm trips at %.2fV, before the %.2fV fault threshold"
      % (limit, cfg["health"]["thresholds"]["volts_min"]))

# One log file per BOOT. Restarts inside a boot append to it, so a crash loop
# leaves one readable file rather than a directory full of fragments.
with tempfile.TemporaryDirectory() as tmp:
    logs = Path(tmp) / "logs"
    cfg2 = deep_merge(DEFAULTS, {"health": {
        "nt_enabled": False, "log_dir": str(logs),
        "state_dir": str(Path(tmp) / "state")}})

    real_boot_id = hm.read_boot_id
    try:
        hm.read_boot_id = lambda: "boot-A"
        restarts = RESTART_LOOP_WARN + 1
        for i in range(restarts):
            mon = HealthMonitor(cfg2, nt_enabled=False)
            mon.buffer.append({"ts": time.time(), "start": mon.state.starts_this_boot})
            mon.flush(force=True)
            if i == 0:
                time.sleep(1.05)     # so a second file would get a different name

        files = sorted(logs.glob("*.jsonl"))
        assert len(files) == 1, [p.name for p in files]
        assert len(files[0].read_text().splitlines()) == restarts
        assert mon.state.starts_this_boot == restarts, mon.state.starts_this_boot
        print("PASS  %d restarts in one boot stay in one file" % restarts)

        # A crash loop has to be visible in the published data, because systemd
        # is told never to give up restarting and so will not flag it.
        assert mon.sample()["starts_this_boot"] >= RESTART_LOOP_WARN
        print("PASS  restart count is published, so a loop is visible")

        # A real reboot starts a new file and resets the counter.
        hm.read_boot_id = lambda: "boot-B"
        mon2 = HealthMonitor(cfg2, nt_enabled=False)
        mon2.buffer.append({"ts": time.time(), "start": 1})
        mon2.flush(force=True)
        assert len(list(logs.glob("*.jsonl"))) == 2
        assert mon2.state.starts_this_boot == 1
        assert mon2.log_path != files[0]
        print("PASS  a real reboot starts a new file and resets the count")

        # The previous boot's file is what gets read back, not this one.
        prev = read_last_records(logs, exclude=mon2.log_path)
        assert prev and prev[-1]["start"] == restarts, prev
        print("PASS  the previous boot's file is what gets read back")
    finally:
        hm.read_boot_id = real_boot_id

print("\nALL PASS")
