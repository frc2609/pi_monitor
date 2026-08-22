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

from health_monitor import DEFAULTS, HealthMonitor, deep_merge, read_last_records


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
    with (logs / "health-20260822.jsonl").open("w") as f:
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

print("\nALL PASS")
