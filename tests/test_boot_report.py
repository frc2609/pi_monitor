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
    with (logs / "health_b00001_20260822_101500.jsonl").open("w") as f:
        for i, v in enumerate(volts):
            f.write(json.dumps({
                "ts": now - (len(volts) - i), "volts_in": v,
                "temp_c": 55.0, "load1": 0.4, "ok": v >= 4.75,
            }) + "\n")
    cfg = deep_merge(DEFAULTS, {
        "nt": {"enabled": False},
        "storage": {"log_dir": str(logs), "state_dir": str(Path(tmp) / "state")},
    })
    return HealthMonitor(cfg, nt_enabled=False, read_only=True), logs


def report(mon, unclean=True):
    cap = Captured()
    log = logging.getLogger("health_monitor")
    previous = log.level
    log.addHandler(cap)
    log.setLevel(logging.DEBUG)   # the "normal power-off" verdict logs at INFO
    try:
        mon.state.last_boot_was_unclean = unclean
        mon.report_previous_boot()
    finally:
        log.removeHandler(cap)
        log.setLevel(previous)
    return cap.text()


# Voltage falling into the cut -> brownout.
with tempfile.TemporaryDirectory() as tmp:
    mon, logs = monitor_with_log(tmp, [5.10, 5.02, 4.93, 4.81, 4.68])
    out = report(mon)
    assert "BROWNOUT" in out, out
    assert "4.68V" in out, out
    assert mon.state.brownout_boots == 1, mon.state.brownout_boots
    assert len(read_last_records(logs)) == 5
    print("PASS  falling voltage reads as a brownout, and is counted")

# Healthy right up to the cut -> something disconnected it.
with tempfile.TemporaryDirectory() as tmp:
    mon, _ = monitor_with_log(tmp, [5.10, 5.09, 5.10, 5.09, 5.10])
    out = report(mon)
    assert "normal power-off" in out, out
    assert "BROWNOUT" not in out, out
    # This is the case that matters on a robot: killing the main breaker never
    # sends SIGTERM, so every boot is "unclean". It must not count as a fault.
    assert mon.state.brownout_boots == 0, mon.state.brownout_boots
    print("PASS  steady voltage reads as a normal power-off, not counted")

# Real readings from a clean power-off: 5.168 down to 5.161, which prints as a
# flat 5.16 but is technically decreasing. "volts[-1] < volts[0]" called that a
# brownout, so half of all normal power-offs were mislabelled by coin flip.
with tempfile.TemporaryDirectory() as tmp:
    mon, _ = monitor_with_log(tmp, [5.168, 5.172, 5.166, 5.163, 5.161])
    out = report(mon)
    assert "BROWNOUT" not in out, out
    assert mon.state.brownout_boots == 0
    print("PASS  sub-millivolt noise is not a brownout")

# A fast sag where only the final sample is low must still be caught.
with tempfile.TemporaryDirectory() as tmp:
    mon, _ = monitor_with_log(tmp, [5.16, 5.17, 5.16, 5.16, 4.85])
    assert "BROWNOUT" in report(mon)
    print("PASS  a sag caught only in the last sample still counts")

# Unclean boot, but the log did not survive.
with tempfile.TemporaryDirectory() as tmp:
    cfg = deep_merge(DEFAULTS, {"nt": {"enabled": False},
        "storage": {"log_dir": str(Path(tmp) / "gone"), "state_dir": str(Path(tmp) / "state")}})
    out = report(HealthMonitor(cfg, nt_enabled=False, read_only=True))
    assert "no log records survived" in out, out
    print("PASS  missing log is reported, not crashed on")

# A clean boot must say nothing at all.
with tempfile.TemporaryDirectory() as tmp:
    mon, _ = monitor_with_log(tmp, [5.10, 5.10])
    assert report(mon, unclean=False) == ""
    print("PASS  clean boot stays quiet")

# Pre-alarm: flush while the voltage is still falling, before it fails.
cfg = deep_merge(DEFAULTS, {"nt": {"enabled": False}, "storage": {"log_dir": None}})
mon = HealthMonitor(cfg, nt_enabled=False, read_only=True)
limit = cfg["thresholds"]["volts_prealarm"]
assert not mon.prealarm({"volts_in": limit + 0.10})
assert mon.prealarm({"volts_in": limit})
assert mon.prealarm({"volts_in": limit - 0.10})
assert not mon.prealarm({"volts_in": None})   # no reading is not an alarm
print("PASS  pre-alarm trips at %.2fV, before the %.2fV fault threshold"
      % (limit, cfg["thresholds"]["volts_min"]))

# One log file per BOOT. Restarts inside a boot append to it, so a crash loop
# leaves one readable file rather than a directory full of fragments.
with tempfile.TemporaryDirectory() as tmp:
    logs = Path(tmp) / "logs"
    cfg2 = deep_merge(DEFAULTS, {"nt": {"enabled": False},
        "storage": {"log_dir": str(logs), "state_dir": str(Path(tmp) / "state")}})

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


# Pruning is by size, not by file count, and that matters most in the failure
# case: a boot that browns out after twenty seconds writes almost nothing, so a
# storm makes many tiny files. A count rule would bin every real boot to keep
# the fragments.
import os   # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    logs = Path(tmp) / "logs"
    logs.mkdir(parents=True)
    now = time.time()

    def make(name, size, age):
        f = logs / name
        f.write_text("x" * size)
        os.utime(f, (now - age, now - age))
        return f

    for i in range(1, 21):                       # 20 real boots, 100KB each
        make("health_b%05d_20260823_1000%02d.jsonl" % (i, i), 100_000, 10_000 - i)
    for i in range(21, 51):                      # a storm: 30 fragments, 2KB each
        make("health_b%05d_20260823_2000%02d.jsonl" % (i, i), 2_000, 100 - i)

    cfg2 = deep_merge(DEFAULTS, {"nt": {"enabled": False}, "storage": {
        "log_dir": str(logs), "state_dir": str(Path(tmp) / "state"),
        "max_log_mb": 1}})
    mon = HealthMonitor(cfg2, nt_enabled=False)
    mon.log_path.write_text("current boot")   # exists once something flushes
    previous = make("health_b00099_20260823_235959.jsonl", 400_000, 20_000)
    mon.state.previous_log_file = str(previous)

    mon.prune_logs()

    left = list(logs.glob("*.jsonl"))
    total = sum(p.stat().st_size for p in left)
    assert total <= 1024 * 1024, total
    assert mon.log_path.exists(), "must never delete the current boot's file"
    assert previous.exists(), "must never delete the previous boot's file"
    fragments = [p for p in left
                 if p.stat().st_size < 5_000 and p != mon.log_path]
    assert len(fragments) == 30, len(fragments)
    print("PASS  pruning by size keeps all 30 storm fragments, drops old bulk")
    print("PASS  current and previous boot files are never pruned")
print("\nALL PASS")
