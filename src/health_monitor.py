#!/usr/bin/env python3
"""
Vision coprocessor — health monitor / power watchdog.

Runs as its own systemd service, deliberately NOT as a child of the vision
process. If a brownout kills PhotonVision, this is what survives to say why.

What it watches:
  - vcgencmd get_throttled       undervoltage / capping / throttling, now and since boot
  - vcgencmd measure_temp        SoC temperature
  - vcgencmd measure_clock arm   actual ARM clock (proves capping, not just the flag)
  - vcgencmd pmic_read_adc       real rail volts and amps, not just fault flags
  - boot id + clean-shutdown marker   distinguishes a brownout reset from a clean stop
  - memory, disk, load, uptime
  - systemd state of the vision services

Logging discipline (per operating standards §10):
  - log on change, not every sample
  - buffer records in RAM, flush to disk on an interval
  - log the *inputs* to a decision, not just the verdict

NetworkTables: numbers and booleans only. Never formatted strings.

Usage:
    health_monitor.py --config /home/pi/vision/monitor/config/config.yaml
    health_monitor.py --once          # one human-readable sample, then exit
    health_monitor.py --once --json   # one machine-readable sample, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("health-monitor")

# ---------------------------------------------------------------------------
# Defaults. Everything here is overridable from config.yaml under `health:`.
# Nothing team-specific or environment-specific belongs in this file.
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "team": None,                     # set in config.yaml; only used by NT client mode
    "nt_mode": "server",              # "server" on the bench, "client" on the robot
    "nt_server": None,                # explicit host/IP; overrides team lookup
    "health": {
        "interval_s": 1.0,
        "nt_enabled": True,
        "nt_table": "Vision/health",
        "state_dir": "/var/lib/health-monitor",
        "log_dir": None,              # None => no disk logging, journald only
        "flush_interval_s": 60.0,
        "heartbeat_log_s": 300.0,     # periodic "still fine" line even with no change
        "buffer_max_records": 2000,
        "log_level": "INFO",        # DEBUG/INFO/WARNING/ERROR; --verbose overrides
        "watch_services": ["photonvision"],
        "service_check_every_n": 10,  # systemctl is slow-ish; don't do it every sample
        "thresholds": {
            "temp_warn_c": 70.0,
            "temp_crit_c": 80.0,
            "volts_min": 4.75,
            "mem_available_min_mb": 200.0,
            "disk_free_min_mb": 500.0,
        },
        # Hysteresis for change detection on continuous values. Without this,
        # temperature noise alone would produce a log line every second.
        "deltas": {
            "temp_c": 2.0,
            "volts": 0.05,
            "power_w": 0.5,
            "cpu_mhz": 100.0,
            "mem_mb": 100.0,
            "disk_mb": 100.0,
        },
    },
}

THROTTLE_BITS = {
    0: ("undervoltage_now", "under-voltage RIGHT NOW"),
    1: ("freq_capped_now", "ARM frequency capped now"),
    2: ("throttled_now", "throttled now"),
    3: ("soft_temp_now", "soft temperature limit active now"),
    16: ("undervoltage_since_boot", "under-voltage has occurred"),
    17: ("freq_capped_since_boot", "frequency capping has occurred"),
    18: ("throttled_since_boot", "throttling has occurred"),
    19: ("soft_temp_since_boot", "soft temperature limit has occurred"),
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None) -> dict:
    if not path:
        return DEFAULTS
    p = Path(path)
    if not p.is_file():
        LOG.warning("config not found at %s — using built-in defaults", p)
        return DEFAULTS
    try:
        import yaml  # type: ignore
    except ImportError:
        LOG.error("PyYAML not installed; ignoring %s and using defaults "
                  "(fix: sudo apt install python3-yaml)", p)
        return DEFAULTS
    with p.open() as f:
        user = yaml.safe_load(f) or {}
    return deep_merge(DEFAULTS, user)


# ---------------------------------------------------------------------------
# Raw readers
# ---------------------------------------------------------------------------

def vcgencmd(*args: str, timeout: float = 3.0) -> str | None:
    """Run vcgencmd. Returns stripped stdout, or None if it isn't usable."""
    try:
        r = subprocess.run(["vcgencmd", *args], capture_output=True,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        LOG.warning("vcgencmd %s timed out", " ".join(args))
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def read_throttled() -> int | None:
    out = vcgencmd("get_throttled")
    if not out:
        return None
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


def decode_throttled(raw: int) -> dict[str, bool]:
    return {name: bool(raw & (1 << bit)) for bit, (name, _) in THROTTLE_BITS.items()}


def describe_throttled(raw: int) -> str:
    hits = [desc for bit, (_, desc) in sorted(THROTTLE_BITS.items()) if raw & (1 << bit)]
    return f"0x{raw:x} (" + ("; ".join(hits) if hits else "clean") + ")"


def read_temp_c() -> float | None:
    out = vcgencmd("measure_temp")
    if out:
        m = re.search(r"temp=([0-9.]+)", out)
        if m:
            return float(m.group(1))
    try:  # fallback for non-vcgencmd environments
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except OSError:
        return None


def read_cpu_mhz() -> float | None:
    out = vcgencmd("measure_clock", "arm")
    if out:
        m = re.search(r"=(\d+)", out)
        if m:
            return int(m.group(1)) / 1e6
    try:
        khz = int(Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read_text())
        return khz / 1000.0
    except OSError:
        return None


PMIC_LINE = re.compile(r"\s*(\S+?)_(A|V)\s+(?:current|volt)\(\d+\)=([0-9.]+)[AV]")


def read_pmic() -> dict[str, Any]:
    """
    Parse `vcgencmd pmic_read_adc` into per-rail volts/amps.

    Output looks like:
        3V3_SYS_A current(1)=0.02514000A
        3V3_SYS_V volt(2)=3.30840000V
        EXT5V_V volt(24)=5.09680000V

    EXT5V_V is the input rail and has no paired current channel, so total draw
    is estimated by summing V*I across the rails that do pair up. It's an
    estimate, not a wall-plug measurement — but it moves when the load moves,
    which is what matters for spotting a supply that can't keep up.
    """
    out = vcgencmd("pmic_read_adc")
    result: dict[str, Any] = {"volts_in": None, "power_w": None, "rails": {}}
    if not out:
        return result

    rails: dict[str, dict[str, float]] = {}
    for line in out.splitlines():
        m = PMIC_LINE.match(line)
        if not m:
            continue
        rail, kind, value = m.group(1), m.group(2), float(m.group(3))
        rails.setdefault(rail, {})["amps" if kind == "A" else "volts"] = value

    total = 0.0
    have_any = False
    for rail, vals in rails.items():
        if "volts" in vals and "amps" in vals:
            total += vals["volts"] * vals["amps"]
            have_any = True

    result["rails"] = rails
    result["volts_in"] = rails.get("EXT5V", {}).get("volts")
    result["power_w"] = round(total, 3) if have_any else None
    return result


def read_uptime_s() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        return None


def read_mem_available_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        pass
    return None


def read_disk_free_mb(path: str = "/") -> float | None:
    try:
        return shutil.disk_usage(path).free / (1024.0 * 1024.0)
    except OSError:
        return None


def read_load1() -> float | None:
    """
    1-minute load average. Absent on Windows, where os.getloadavg() does not
    exist at all — laptop development must not die on a missing metric.
    """
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def read_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def service_active(name: str) -> bool | None:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", name],
                           capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Persistent state — boot counting and unclean-shutdown detection
# ---------------------------------------------------------------------------

class BootState:
    """
    Survives reboots so we can tell a clean stop from a power cut.

    On a robot, `uptime` alone lies to you: a Pi that browned out and rebooted
    mid-match looks identical to one that just started. Comparing the kernel's
    boot_id against what we saw last run — plus a marker we only write on a
    clean SIGTERM — makes that distinction visible.
    """

    def __init__(self, state_dir: str, read_only: bool = False):
        # read_only exists for --once. A one-shot inspection that wrote
        # clean_shutdown=false and then exited would make the *next* power
        # cycle get recorded as a brownout that never happened.
        self.read_only = read_only
        self.path = Path(state_dir) / "boot_state.json"
        self.boot_id = read_boot_id()
        self.boot_count = 0
        self.unclean_boots = 0
        self.last_boot_was_unclean = False
        self._load_and_update()

    def _load_and_update(self) -> None:
        prev: dict[str, Any] = {}
        try:
            prev = json.loads(self.path.read_text())
        except (OSError, ValueError):
            LOG.info("no previous boot state at %s — starting a fresh record", self.path)

        self.boot_count = int(prev.get("boot_count", 0))
        self.unclean_boots = int(prev.get("unclean_boots", 0))
        prev_boot_id = prev.get("boot_id")
        prev_clean = bool(prev.get("clean_shutdown", True))

        if prev_boot_id != self.boot_id:
            # Genuinely a new boot, not just a service restart within one boot.
            self.boot_count += 1
            if prev_boot_id is not None and not prev_clean:
                self.unclean_boots += 1
                self.last_boot_was_unclean = True
                LOG.warning(
                    "previous boot ended without a clean shutdown "
                    "(prev_boot_id=%s, clean_marker=false) — power cut or brownout reset. "
                    "unclean_boots=%d", prev_boot_id, self.unclean_boots)
        else:
            LOG.info("service restarted within the same boot (boot_id unchanged)")

        self._write(clean=False)

    def _write(self, clean: bool) -> None:
        if self.read_only:
            return
        payload = {
            "boot_id": self.boot_id,
            "boot_count": self.boot_count,
            "unclean_boots": self.unclean_boots,
            "clean_shutdown": clean,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self.path)   # atomic; a half-written state file is worse than none
        except OSError as e:
            LOG.error("could not write boot state to %s: %s", self.path, e)

    def mark_clean(self) -> None:
        self._write(clean=True)


# ---------------------------------------------------------------------------
# NetworkTables
# ---------------------------------------------------------------------------

class NTPublisher:
    """
    Publishes flat numbers and booleans under the health table.

    Strings are dropped on purpose — they don't graph in AdvantageScope and
    can't be consumed without parsing.
    """

    def __init__(self, cfg: dict):
        h = cfg["health"]
        self.enabled = bool(h["nt_enabled"])
        self.pubs: dict[str, Any] = {}
        self.table = None
        self.inst = None
        if not self.enabled:
            return
        try:
            import ntcore  # type: ignore
        except ImportError:
            LOG.error("ntcore not installed — NT publishing disabled "
                      "(fix: pip install -r requirements.txt)")
            self.enabled = False
            return

        self.ntcore = ntcore
        self.inst = ntcore.NetworkTableInstance.getDefault()
        mode = str(cfg.get("nt_mode", "server")).lower()
        if mode == "client":
            self.inst.startClient4("health-monitor")
            if cfg.get("nt_server"):
                self.inst.setServer(str(cfg["nt_server"]))
                LOG.info("NT client → %s", cfg["nt_server"])
            else:
                team = cfg.get("team")
                if team:
                    self.inst.setServerTeam(int(team))
                    LOG.info("NT client → team %s", team)
                else:
                    # Loud, because the symptom is otherwise just "no data in
                    # AdvantageScope" with a perfectly healthy-looking service.
                    LOG.error("nt_mode is client but neither nt_server nor team is "
                              "set - NT has nothing to connect to. Set `team:` in "
                              "config.yaml.")
        else:
            self.inst.startServer()
            LOG.info("NT server started (bench mode)")
        self.table = self.inst.getTable(h["nt_table"])

    def publish(self, flat: dict[str, Any]) -> None:
        if not self.enabled or self.table is None:
            return
        for key, value in flat.items():
            if value is None or isinstance(value, str):
                continue
            pub = self.pubs.get(key)
            if pub is None:
                if isinstance(value, bool):
                    pub = self.table.getBooleanTopic(key).publish()
                elif isinstance(value, (int, float)):
                    pub = self.table.getDoubleTopic(key).publish()
                else:
                    continue
                self.pubs[key] = pub
            try:
                pub.set(value if isinstance(value, bool) else float(value))
            except Exception as e:  # never let a publish failure kill the watchdog
                LOG.debug("publish failed for %s: %s", key, e)

    def close(self) -> None:
        if not self.enabled or self.inst is None:
            return
        for pub in self.pubs.values():
            try:
                pub.close()
            except Exception:
                pass
        try:
            self.inst.stopClient()
            self.inst.stopServer()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class HealthMonitor:
    def __init__(self, cfg: dict, nt_enabled: bool = True, read_only: bool = False):
        self.cfg = cfg
        self.h = cfg["health"]
        self.thresholds = self.h["thresholds"]
        self.deltas = self.h["deltas"]

        self.state = BootState(self.h["state_dir"], read_only=read_only)
        if not nt_enabled:
            self.h = dict(self.h)
            self.h["nt_enabled"] = False
            cfg = deep_merge(cfg, {"health": {"nt_enabled": False}})
        self.nt = NTPublisher(cfg)

        self.buffer: deque[dict] = deque(maxlen=int(self.h["buffer_max_records"]))
        self.log_dir = Path(self.h["log_dir"]) if self.h.get("log_dir") else None
        self.last_flush = time.monotonic()
        self.last_heartbeat_log = 0.0
        self.last_signature: tuple | None = None

        # Throttle bits latched at start-of-service, so we can report "since I
        # started watching" separately from "since boot". The since-boot bits
        # persist across soft reboots and will happily mislead you into
        # re-debugging yesterday's brownout.
        self.baseline_throttled: int | None = None
        self.sticky_since_start = 0
        self.service_cache: dict[str, bool | None] = {}
        self.sample_count = 0
        self.heartbeat = 0
        self.running = True

    # -- sampling ---------------------------------------------------------

    def sample(self) -> dict[str, Any]:
        self.sample_count += 1
        self.heartbeat += 1

        raw = read_throttled()
        bits = decode_throttled(raw) if raw is not None else {}

        if raw is not None:
            if self.baseline_throttled is None:
                self.baseline_throttled = raw
                if raw:
                    LOG.warning("throttle flags already set at service start: %s "
                                "— these are historical, from before this process "
                                "existed. Full power cycle clears them.",
                                describe_throttled(raw))
            # Bits that appeared after we started watching. This is the number
            # that means something during a match.
            self.sticky_since_start |= (raw & ~self.baseline_throttled)

        since_start = decode_throttled(self.sticky_since_start)
        pmic = read_pmic()
        temp = read_temp_c()
        mem_mb = read_mem_available_mb()
        disk_mb = read_disk_free_mb()

        if self.sample_count % int(self.h["service_check_every_n"]) == 1 or not self.service_cache:
            for name in self.h["watch_services"]:
                self.service_cache[name] = service_active(name)

        s: dict[str, Any] = {
            "ts": time.time(),
            "heartbeat": self.heartbeat,
            "throttled_raw": raw,
            "undervoltage_now": bits.get("undervoltage_now"),
            "freq_capped_now": bits.get("freq_capped_now"),
            "throttled_now": bits.get("throttled_now"),
            "soft_temp_now": bits.get("soft_temp_now"),
            "undervoltage_since_boot": bits.get("undervoltage_since_boot"),
            "throttled_since_boot": bits.get("throttled_since_boot"),
            "undervoltage_since_start": since_start.get("undervoltage_since_boot")
                                        or since_start.get("undervoltage_now"),
            "throttled_since_start": since_start.get("throttled_since_boot")
                                      or since_start.get("throttled_now"),
            "temp_c": temp,
            "cpu_mhz": read_cpu_mhz(),
            "volts_in": pmic["volts_in"],
            "power_w": pmic["power_w"],
            "mem_available_mb": round(mem_mb, 1) if mem_mb is not None else None,
            "disk_free_mb": round(disk_mb, 1) if disk_mb is not None else None,
            "load1": read_load1(),
            "uptime_s": read_uptime_s(),
            "boot_count": self.state.boot_count,
            "unclean_boots": self.state.unclean_boots,
            "last_boot_unclean": self.state.last_boot_was_unclean,
        }
        for name, active in self.service_cache.items():
            s[f"svc_{name.replace('-', '_')}_active"] = active

        s["ok"] = self.verdict(s)
        return s

    def verdict(self, s: dict[str, Any]) -> bool:
        t = self.thresholds
        checks = [
            not s.get("undervoltage_now"),
            not s.get("throttled_now"),
            not s.get("undervoltage_since_start"),
            not s.get("throttled_since_start"),
            s.get("temp_c") is None or s["temp_c"] < t["temp_crit_c"],
            s.get("volts_in") is None or s["volts_in"] >= t["volts_min"],
            s.get("mem_available_mb") is None or s["mem_available_mb"] >= t["mem_available_min_mb"],
            s.get("disk_free_mb") is None or s["disk_free_mb"] >= t["disk_free_min_mb"],
        ]
        checks += [v is not False for k, v in s.items() if k.startswith("svc_")]
        return all(checks)

    # -- change detection -------------------------------------------------

    def signature(self, s: dict[str, Any]) -> tuple:
        """
        Bucket the continuous values so noise doesn't produce a log line every
        second, while any real movement still does.
        """
        d = self.deltas

        def bucket(value, size):
            return None if value is None else int(value / size)

        booleans = tuple(sorted(
            (k, v) for k, v in s.items()
            if isinstance(v, bool) and k not in ("last_boot_unclean",)
        ))
        return (
            booleans,
            s.get("throttled_raw"),
            bucket(s.get("temp_c"), d["temp_c"]),
            bucket(s.get("volts_in"), d["volts"]),
            bucket(s.get("power_w"), d["power_w"]),
            bucket(s.get("cpu_mhz"), d["cpu_mhz"]),
            bucket(s.get("mem_available_mb"), d["mem_mb"]),
            bucket(s.get("disk_free_mb"), d["disk_mb"]),
        )

    def log_sample(self, s: dict[str, Any], reason: str) -> None:
        """Log the inputs to the verdict, never just the verdict."""
        parts = [
            f"ok={s['ok']}",
            f"throttled={describe_throttled(s['throttled_raw']) if s['throttled_raw'] is not None else 'n/a'}",
            f"temp={fmt(s['temp_c'], '°C')}",
            f"vin={fmt(s['volts_in'], 'V')}",
            f"pwr={fmt(s['power_w'], 'W')}",
            f"cpu={fmt(s['cpu_mhz'], 'MHz', 0)}",
            f"mem={fmt(s['mem_available_mb'], 'MB', 0)}",
            f"disk={fmt(s['disk_free_mb'], 'MB', 0)}",
            f"load={fmt(s['load1'], '', 2)}",
            f"up={fmt(s['uptime_s'], 's', 0)}",
            f"boots={s['boot_count']}/{s['unclean_boots']}unclean",
        ]
        parts += [f"{k}={v}" for k, v in s.items() if k.startswith("svc_")]
        line = f"[{reason}] " + " ".join(parts)

        t = self.thresholds
        if not s["ok"]:
            LOG.error(line)
        elif (s.get("temp_c") or 0) >= t["temp_warn_c"] or s.get("last_boot_unclean"):
            LOG.warning(line)
        else:
            LOG.info(line)

    # -- disk buffer ------------------------------------------------------

    def buffer_record(self, s: dict[str, Any]) -> None:
        if self.log_dir is None:
            return
        self.buffer.append(s)

    def flush(self, force: bool = False) -> None:
        if self.log_dir is None or not self.buffer:
            return
        now = time.monotonic()
        if not force and (now - self.last_flush) < float(self.h["flush_interval_s"]):
            return
        self.last_flush = now
        day = datetime.now().strftime("%Y%m%d")
        path = self.log_dir / f"health-{day}.jsonl"
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                while self.buffer:
                    f.write(json.dumps(self.buffer.popleft(), default=str) + "\n")
        except OSError as e:
            LOG.error("could not flush health log to %s: %s", path, e)
            self.buffer.clear()  # don't grow unbounded chasing a dead card

    # -- main loop --------------------------------------------------------

    def stop(self, signum=None, frame=None) -> None:
        LOG.info("stopping on signal %s — marking clean shutdown", signum)
        self.running = False

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        interval = float(self.h["interval_s"])
        heartbeat_log_s = float(self.h["heartbeat_log_s"])
        LOG.info("health-monitor starting: interval=%.2fs table=%s state=%s log_dir=%s",
                 interval, self.h["nt_table"], self.h["state_dir"], self.h["log_dir"])

        next_t = time.monotonic()
        while self.running:
            try:
                s = self.sample()
                self.nt.publish(s)

                sig = self.signature(s)
                now = time.monotonic()
                if sig != self.last_signature:
                    self.log_sample(s, "change" if self.last_signature is not None else "baseline")
                    self.buffer_record(s)
                    self.last_signature = sig
                    self.last_heartbeat_log = now
                    if not s["ok"]:
                        self.flush(force=True)  # get the evidence onto disk now
                elif (now - self.last_heartbeat_log) >= heartbeat_log_s:
                    self.log_sample(s, "heartbeat")
                    self.buffer_record(s)
                    self.last_heartbeat_log = now

                self.flush()
            except Exception:
                LOG.exception("sample failed — continuing")

            next_t += interval
            sleep = next_t - time.monotonic()
            if sleep < 0:                 # we fell behind; resync rather than spin
                next_t = time.monotonic()
                sleep = 0
            time.sleep(sleep)

        self.flush(force=True)
        self.state.mark_clean()
        self.nt.close()
        LOG.info("health-monitor stopped cleanly")
        return 0


def fmt(value, unit="", places=1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{places}f}{unit}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def print_once(mon: HealthMonitor, as_json: bool) -> None:
    s = mon.sample()
    if as_json:
        print(json.dumps(s, indent=2, default=str))
        return
    print(f"verdict            : {'OK' if s['ok'] else 'PROBLEM'}")
    raw = s["throttled_raw"]
    print(f"get_throttled      : {describe_throttled(raw) if raw is not None else 'n/a'}")
    print(f"  since service up : undervoltage={s['undervoltage_since_start']} "
          f"throttled={s['throttled_since_start']}")
    print(f"temperature        : {fmt(s['temp_c'], ' °C')}")
    print(f"arm clock          : {fmt(s['cpu_mhz'], ' MHz', 0)}")
    print(f"input voltage      : {fmt(s['volts_in'], ' V', 2)}")
    print(f"estimated draw     : {fmt(s['power_w'], ' W', 2)}")
    print(f"memory available   : {fmt(s['mem_available_mb'], ' MB', 0)}")
    print(f"disk free          : {fmt(s['disk_free_mb'], ' MB', 0)}")
    print(f"load (1m)          : {fmt(s['load1'], '', 2)}")
    print(f"uptime             : {fmt(s['uptime_s'], ' s', 0)}")
    print(f"boots / unclean    : {s['boot_count']} / {s['unclean_boots']}"
          + ("   <-- last boot was NOT clean" if s["last_boot_unclean"] else ""))
    for k, v in s.items():
        if k.startswith("svc_"):
            print(f"{k:<19}: {v}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Vision coprocessor health monitor")
    ap.add_argument("--config", default=os.environ.get("VISION_CONFIG"),
                    help="path to config.yaml")
    ap.add_argument("--interval", type=float, help="override sample interval, seconds")
    ap.add_argument("--once", action="store_true", help="print one sample and exit")
    ap.add_argument("--json", action="store_true", help="with --once, emit JSON")
    ap.add_argument("--no-nt", action="store_true", help="disable NetworkTables")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
    )

    cfg = load_config(args.config)
    if not args.verbose:
        level = str(cfg["health"].get("log_level", "INFO")).upper()
        logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
    if args.interval:
        cfg = deep_merge(cfg, {"health": {"interval_s": args.interval}})
    if args.once:
        cfg = deep_merge(cfg, {"health": {"nt_enabled": False, "log_dir": None}})

    mon = HealthMonitor(cfg, nt_enabled=not (args.no_nt or args.once),
                        read_only=args.once)

    if args.once:
        print_once(mon, args.json)
        return 0
    return mon.run()


if __name__ == "__main__":
    sys.exit(main())
