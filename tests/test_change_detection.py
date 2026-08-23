"""
Change-detection regression tests for the health monitor.

Runs anywhere - no Pi, no vcgencmd, no NetworkTables:

    python3 tests/test_change_detection.py

Guards the property that makes the log readable: sensor noise must not
produce log lines, and real movement must.
"""
import sys
sys.path.insert(0, "src")

from health_monitor import DEFAULTS, HealthMonitor, deep_merge

cfg = deep_merge(DEFAULTS, {"nt": {"enabled": False}, "storage": {"log_dir": None}})
mon = HealthMonitor(cfg, nt_enabled=False, read_only=True)

BASE = {
    "throttled_raw": 0,
    "undervoltage_now": False,
    "throttled_now": False,
    "ok": True,
    "temp_c": 55.0,
    "cpu_mhz": 1500.0,
    "power_w": 6.0,
    "mem_available_mb": 7000.0,
    "disk_free_mb": 20000.0,
}


def sample(**over):
    s = dict(BASE)
    s.update(over)
    return s


def run(label, values, field="volts_in"):
    """Feed a series of readings; count how many would have been logged."""
    mon.last_discrete = None
    mon.last_logged = {}
    logged = 0
    for v in values:
        s = sample(**{field: v})
        if mon.changed(s):
            logged += 1
            mon.remember(s)
    return logged


delta = cfg["deltas"]["volts"]
print(f"volts delta = {delta}")
print()

# 4.900 / 0.05 == 98.0 exactly - the old int(v/size) bucketing sat right here,
# so a millivolt of noise flipped bucket 97 <-> 98 on every single sample.
chatter = [4.900, 4.8999, 4.9001, 4.8998, 4.9002, 4.8999, 4.9001,
           4.900, 4.8999, 4.9001, 4.8998, 4.9002, 4.8999, 4.9001]
n = run("chatter", chatter)
old_buckets = len({int(v / delta) for v in chatter})
print(f"A rail parked on the 4.900V bucket boundary, +/-0.2mV of noise:")
print(f"  samples fed                     : {len(chatter)}")
print(f"  distinct old grid buckets hit   : {old_buckets}  (each crossing logged a line)")
print(f"  lines logged now                : {n}  (expect 1 - the baseline)")
assert n == 1, f"boundary chatter still logs {n} times"
print("  PASS - noise no longer produces log lines")
print()

# A genuine sag must still be caught, including one that creeps.
sag = [5.10, 5.09, 5.05, 4.99, 4.90, 4.80, 4.70, 4.60]
n = run("sag", sag)
print(f"A real sag {sag[0]}V -> {sag[-1]}V:")
print(f"  lines logged                    : {n}  (expect several)")
assert n >= 5, f"real movement under-logged: {n}"
print("  PASS - genuine movement still logs")
print()

# Movement smaller than the delta, but accumulating past it, must eventually log.
creep = [5.00 - 0.01 * i for i in range(12)]      # 10mV steps, delta is 20mV
n = run("creep", creep)
print(f"A slow creep in 10mV steps (delta {delta}V), 5.00V -> {creep[-1]:.2f}V:")
print(f"  lines logged                    : {n}")
# Roughly (total drift / delta) crossings, plus the baseline. Asserted as a
# range rather than an exact count on purpose: 10mV steps against a 20mV
# threshold land exactly on the >= comparison, where binary floating point
# decides the tie for us. 5.00 - 0.04 is 4.959999999999999, so the gap from
# 4.98 measures 0.019999999999999574 and just misses. That is a property of
# floats, not of the monitor, and pinning an exact number here would make the
# test fail for the wrong reason.
expected = 1 + abs(creep[-1] - creep[0]) / delta
assert expected - 2 <= n <= expected + 1, (
    f"expected about {expected:.0f} lines for this drift, got {n}")
print("  PASS - drift accumulates against the last LOGGED value, so it is caught")
print("         (fixed-grid bucketing would also catch this, but only by luck")
print("          of where the grid lines happen to fall)")
print()

# A reading disappearing (sensor read failure) is itself news.
mon.last_discrete = None
mon.last_logged = {}
assert mon.changed(sample(volts_in=5.0))
mon.remember(sample(volts_in=5.0))
assert mon.changed(sample(volts_in=None)), "a vanishing reading must log"
print("A reading vanishing (vcgencmd failure): PASS - logged")
print()
print("ALL PASS")
