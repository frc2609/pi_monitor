#!/usr/bin/env python3
"""
Vision coprocessor - health monitor / power watchdog.

Runs as its own systemd service, NOT as a child of the vision process. If a
brownout kills PhotonVision, this is what survives to say why.

Watches: get_throttled (undervoltage and throttling, now and since boot),
temperature, actual ARM clock, PMIC volts and amps, boot id against a
clean-shutdown marker, memory, disk, load, uptime, and whether the vision
services are running.

Logs on change, buffers in RAM, and records the inputs to a decision rather
than just the verdict. Publishes numbers and booleans to NetworkTables -
never formatted strings, which don't graph.

Usage:
    health_monitor.py --config /home/pi/vision/monitor/config/config.yaml
    health_monitor.py --once          # one readable sample, then exit
    health_monitor.py --once --json   # same, machine-readable
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("health_monitor")

# ---------------------------------------------------------------------------
# Defaults. Everything here is overridable from config.yaml under `health:`.
# Nothing team-specific or environment-specific belongs in this file.
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "team": 2609,
    "interval_s": 1.0,
    "log_level": "INFO",
    "nt": {
        "mode": "server",             # "server" on the bench, "client" on the robot
        "server": None,               # explicit host/IP; overrides the team lookup
        "enabled": True,
        "table": "Vision/health",
    },
    "storage": {
        "state_dir": "/var/lib/health_monitor",
        "log_dir": None,              # None => no disk logging, journald only
        "flush_interval_s": 10.0,
        "heartbeat_log_s": 10.0,
        "buffer_max_records": 2000,
    },
    "services": {
        "watch": ["photonvision"],
        "check_every_n": 5,           # systemctl is slow-ish; not every sample
    },
    "thresholds": {
        "temp_warn_c": 70.0,
        "temp_crit_c": 80.0,
        "volts_min": 4.75,
        "volts_prealarm": 4.90,
        "mem_available_min_mb": 200.0,
        "disk_free_min_mb": 500.0,
    },
    # How far a reading must move from the last value LOGGED before it counts
    # as news. See changed().
    "deltas": {
        "temp_c": 4.0,
        "volts": 0.05,
        "power_w": 0.5,
        "mem_mb": 100.0,
        "disk_mb": 100.0,
    },
}


# Where each field lands in the NetworkTables tree. A "/" makes AdvantageScope
# nest it, so related values collapse under one heading instead of appearing as
# thirty loose entries.
#
# Only the NT layout changes. The sample dict and the .jsonl file stay flat, so
# jq recipes and the log reader are unaffected.
NT_GROUPS = {
    "throttle": ["throttled_raw", "undervoltage_now", "freq_capped_now",
                 "throttled_now", "soft_temp_now",
                 "undervoltage_since_boot", "throttled_since_boot",
                 "undervoltage_since_start", "throttled_since_start"],
    "power": ["volts_in", "power_w", "supply_max_current_ma",
              "usb_high_current", "usb_over_current"],
    "thermal": ["temp_c", "cpu_mhz"],
    "resources": ["mem_available_mb", "disk_free_mb", "load1"],
    "boot": ["boot_count", "unclean_boots", "brownout_boots", "last_boot_unclean",
             "uptime_s", "starts_this_boot", "restarts_by_systemd"],
    "monitor": ["ts", "log_write_ok", "nt_connected", "nt_offset_us"],
}

# ok and heartbeat are deliberately left at the top so they are the first two
# things you see, and the two worth pinning to a graph.
NT_PATH = {field: group + "/" + field
           for group, fields in NT_GROUPS.items() for field in fields}


def nt_value(key: str, value: Any) -> Any:
    """
    Services publish as numbers rather than booleans.

    A boolean cannot say "not installed" - only true or false - so a service
    that does not exist yet never got a topic at all, and the whole services/
    group was missing from the tree instead of showing as unknown.

        1    running
        0    installed but stopped
        NaN  not installed (AdvantageScope draws a gap)
    """
    if key.startswith("svc_"):
        return float("nan") if value is None else (1.0 if value else 0.0)
    return value


def nt_topic(key: str) -> str:
    """Where a sample field goes in the NT tree."""
    if key.startswith("svc_"):
        name = key[len("svc_"):]
        if name.endswith("_active"):
            name = name[:-len("_active")]
        return "services/" + name
    return NT_PATH.get(key, key)


# Sample field -> key in config `deltas`. Used for change detection.
DELTA_FIELDS = {
    "temp_c": "temp_c",
    "volts_in": "volts",
    "power_w": "power_w",
    "mem_available_mb": "mem_mb",
    "disk_free_mb": "disk_mb",
}

# cpu_mhz is deliberately absent. An idle Pi scales between 1500 and 2400MHz
# constantly, and no tolerance absorbs a 900MHz step - it would log on every
# idle and every wake. It is still sampled, recorded and published; it just
# does not trigger a line on its own.
#
# Nothing is lost: throttling sets the throttle bits, those are in the discrete
# signature, and they log immediately. The clock drop is the corroboration you
# read next to them, not the alarm.

# Starts within one boot before we call it a restart loop. systemd is told
# never to stop restarting us, so nothing else will notice.
RESTART_LOOP_WARN = 5

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
    Parse `vcgencmd pmic_read_adc` into per-rail volts and amps. Lines look
    like `3V3_SYS_A current(1)=0.02514000A` or `EXT5V_V volt(24)=5.09680000V`.

    EXT5V is the input rail and has no current channel, so total draw is
    estimated by summing V*I over the rails that do pair up. An estimate, but
    it moves when the load moves, which is what spots a failing supply.
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


def systemd_restarts() -> int | None:
    """
    How many times systemd has restarted us automatically. None when we are not
    running under systemd, which includes every laptop run.

    This is the honest restart-loop signal. starts_this_boot counts every start
    including your own deploys, so it climbs during setup; NRestarts counts only
    the ones systemd had to perform, and resets when a human starts or stops the
    unit deliberately.

    systemd does not put the unit name in the environment, so the unit file
    passes it in as UNIT_NAME.
    """
    unit = os.environ.get("UNIT_NAME")
    if not unit:
        return None
    try:
        r = subprocess.run(["systemctl", "show", "-p", "NRestarts", "--value", unit],
                           capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return int((r.stdout or "").strip())
    except ValueError:
        return None


DT_POWER = Path("/proc/device-tree/chosen/power")


def read_dt_int(name: str) -> int | None:
    """
    Read one property from the firmware's power node. They are big-endian
    4-byte integers, not text - `cat` shows nothing useful.

    Absent on anything that is not a Pi, which is how this stays laptop-safe.
    """
    try:
        raw = (DT_POWER / name).read_bytes()
    except OSError:
        return None
    if not raw:
        return 1              # a property with no value means "present"
    return int.from_bytes(raw[:4], "big")


def read_supply() -> dict[str, Any]:
    """
    What the firmware negotiated over USB-PD, and whether USB has faulted.

    This is the question a label cannot answer. A "27W" charger usually means
    27W at 9V or 12V; the Pi only draws 5V, and standard USB-PD stops at
    5V/3A. 5000 here means the supply really does offer the 5V/5A profile the
    Pi 5 wants. Below that the board still runs, but USB peripherals are capped
    at 600mA total instead of 1.6A - which is what decides whether a second
    camera works.
    """
    return {
        "supply_max_current_ma": read_dt_int("max_current"),
        "usb_high_current": bool(read_dt_int("usb_max_current_enable") or 0)
        if read_dt_int("usb_max_current_enable") is not None else None,
        "usb_over_current": bool(read_dt_int("usb_over_current_detected") or 0)
        if read_dt_int("usb_over_current_detected") is not None else None,
    }


def read_load1() -> float | None:
    """
    1-minute load average. Absent on Windows, where os.getloadavg() does not
    exist at all — laptop development must not die on a missing metric.
    """
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def sd_notify(message: str) -> bool:
    """
    Tell systemd we are still alive, via $NOTIFY_SOCKET.

    Does nothing when that variable is unset, which is the case whenever the
    monitor is run by hand - so laptop and --once runs are unaffected.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):            # abstract socket namespace
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
        return True
    except (OSError, AttributeError):   # AF_UNIX is absent on Windows
        return False


def read_last_records(log_dir: Path, count: int = 5,
                      exclude: Path | None = None) -> list[dict]:
    """
    Last few records from the newest health log, skipping `exclude` so we never
    read the file this run is writing. Used after an unclean boot to show what
    the Pi was doing just before the power went.
    """
    try:
        files = [p for p in sorted(log_dir.glob("health_*.jsonl")) if p != exclude]
        if not files:
            return []
        with files[-1].open("rb") as f:          # tail, not the whole file
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-count:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def read_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def service_active(name: str) -> bool | None:
    """
    True = running, False = installed but not running, None = not installed.

    The three-way answer matters. `is-active` cannot tell a stopped service
    from one that was never installed, so a unit we simply have not set up yet
    reported as "down" and failed every single sample. PhotonVision does not
    exist until someone installs it, and that is not a fault.
    """
    try:
        r = subprocess.run(
            ["systemctl", "show", "-p", "LoadState", "-p", "ActiveState", name],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    fields = dict(line.split("=", 1)
                  for line in (r.stdout or "").splitlines() if "=" in line)
    if fields.get("LoadState") != "loaded":
        return None          # not-found, masked, or a broken unit file
    return fields.get("ActiveState") == "active"


# ---------------------------------------------------------------------------
# Persistent state — boot counting and unclean-shutdown detection
# ---------------------------------------------------------------------------

class BootState:
    """
    Survives reboots so we can tell a clean stop from a power cut. Uptime
    alone can't: a Pi that browned out mid-match looks like one that just
    started. Comparing the kernel's boot_id against last run, plus a marker
    written only on a clean SIGTERM, makes the difference visible.
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
        self.brownout_boots = 0
        self.starts_this_boot = 1
        self.log_file: str | None = None
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
        self.brownout_boots = int(prev.get("brownout_boots", 0))
        prev_boot_id = prev.get("boot_id")
        prev_clean = bool(prev.get("clean_shutdown", True))
        prev_starts = int(prev.get("starts_this_boot", 0))
        prev_log_file = prev.get("log_file")

        if prev_boot_id != self.boot_id:
            # Genuinely a new boot, not just a service restart within one boot.
            self.boot_count += 1
            if prev_boot_id is not None and not prev_clean:
                self.unclean_boots += 1
                self.last_boot_was_unclean = True
                # On the robot this is EVERY boot - killing the main breaker
                # never sends SIGTERM. The count is power cycles, not faults.
                # brownout_boots below is the number that means something.
                LOG.info("previous boot ended without a clean shutdown "
                         "(prev_boot_id=%s) - normal on a robot, where power is "
                         "cut at the breaker. unclean_boots=%d",
                         prev_boot_id, self.unclean_boots)
        else:
            self.starts_this_boot = prev_starts + 1
            # Same boot, so carry on writing wherever this boot already was.
            self.log_file = prev_log_file
            LOG.info("service restarted within the same boot (start #%d)",
                     self.starts_this_boot)
            if self.starts_this_boot >= RESTART_LOOP_WARN:
                # Deliberately mild: this counts deploys too, so it climbs
                # during setup. restarts_by_systemd is the one that means
                # something died.
                LOG.info("started %d times without a reboot. If those were your "
                         "own restarts this is fine; watch restarts_by_systemd "
                         "for the ones systemd had to do.",
                         self.starts_this_boot)

        self._write(clean=False)

    def _write(self, clean: bool) -> None:
        if self.read_only:
            return
        payload = {
            "boot_id": self.boot_id,
            "boot_count": self.boot_count,
            "unclean_boots": self.unclean_boots,
            "brownout_boots": self.brownout_boots,
            "starts_this_boot": self.starts_this_boot,
            "log_file": self.log_file,
            "clean_shutdown": clean,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            # fsync before the rename, or the rename can land while the
            # contents are still in the page cache.
            with tmp.open("w") as f:
                f.write(json.dumps(payload))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)   # atomic; a half-written state file is worse than none
            try:
                dir_fd = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                pass                     # not supported here (e.g. Windows)
            else:
                try:
                    os.fsync(dir_fd)     # make the rename itself durable
                finally:
                    os.close(dir_fd)
        except OSError as e:
            LOG.error("could not write boot state to %s: %s", self.path, e)

    def mark_clean(self) -> None:
        self._write(clean=True)

    def set_log_file(self, path: Path) -> None:
        """Remember which file this boot is writing to, so a restart carries
        on in the same one rather than guessing from filenames."""
        self.log_file = str(path)
        self._write(clean=False)

    def record_brownout(self) -> None:
        """The previous boot ended in a sag, not in someone cutting power."""
        self.brownout_boots += 1
        self._write(clean=False)


# ---------------------------------------------------------------------------
# NetworkTables
# ---------------------------------------------------------------------------

class NTPublisher:
    """
    Publishes flat numbers and booleans under the health table. Strings are
    dropped on purpose: they don't graph in AdvantageScope.
    """

    def __init__(self, cfg: dict):
        nt = cfg["nt"]
        self.enabled = bool(nt["enabled"])
        self.pubs: dict[str, Any] = {}
        self.numeric: dict[str, bool] = {}   # key -> is it a double topic
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
        mode = str(nt.get("mode", "server")).lower()
        if mode == "client":
            self.inst.startClient4("health_monitor")
            if nt.get("server"):
                self.inst.setServer(str(nt["server"]))
                LOG.info("NT client → %s", nt["server"])
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
        self.table = self.inst.getTable(nt["table"])

    def publish(self, flat: dict[str, Any]) -> None:
        if not self.enabled or self.table is None:
            return
        for key, value in flat.items():
            value = nt_value(key, value)
            if isinstance(value, str):
                continue
            name = nt_topic(key)
            if value is None:
                # NaN rather than skipping. Skipping leaves the topic holding
                # its last value, so a sensor that stopped answering reads as a
                # healthy frozen one. AdvantageScope draws NaN as a gap.
                # Booleans have no NaN, so a missing boolean keeps its last
                # state - there is nothing better to send.
                pub = self.pubs.get(name)
                if pub is not None and self.numeric.get(name):
                    self._set(name, pub, float("nan"))
                continue
            pub = self.pubs.get(name)
            if pub is None:
                if isinstance(value, bool):     # bool before int: bools are ints
                    pub = self.table.getBooleanTopic(name).publish()
                    self.numeric[name] = False
                elif isinstance(value, (int, float)):
                    pub = self.table.getDoubleTopic(name).publish()
                    self.numeric[name] = True
                else:
                    continue
                self.pubs[name] = pub
            self._set(name, pub, value if isinstance(value, bool) else float(value))

    def _set(self, key: str, pub: Any, value: Any) -> None:
        try:
            pub.set(value)
        except Exception as e:  # never let a publish failure kill the watchdog
            LOG.debug("publish failed for %s: %s", key, e)

    def status(self) -> dict[str, Any]:
        """
        Connection state, plus NT4's client/server clock offset in
        microseconds. NT4 syncs the client's clock to the server's, so this is
        what lines our records up with roboRIO logs without touching the OS
        clock.
        """
        out: dict[str, Any] = {"nt_connected": None, "nt_offset_us": None}
        if not self.enabled or self.inst is None:
            return out
        try:
            out["nt_connected"] = bool(self.inst.isConnected())
        except Exception:
            pass
        try:
            offset = self.inst.getServerTimeOffset()
            if offset is not None:
                out["nt_offset_us"] = int(offset)
        except Exception:
            pass       # older ntcore, or server mode where it means nothing
        return out

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
        self.storage = cfg["storage"]
        self.services = cfg["services"]
        self.thresholds = cfg["thresholds"]
        self.deltas = cfg["deltas"]

        self.state = BootState(self.storage["state_dir"], read_only=read_only)
        if not nt_enabled:
            cfg = deep_merge(cfg, {"nt": {"enabled": False}})
        self.nt = NTPublisher(cfg)

        self.buffer: deque[dict] = deque(maxlen=int(self.storage["buffer_max_records"]))
        self.log_dir = Path(self.storage["log_dir"]) if self.storage.get("log_dir") else None
        self.log_path = self.log_file_for_this_boot()
        self.last_flush = time.monotonic()
        self.last_heartbeat_log = 0.0
        self.log_write_ok = True
        self.last_described: int | None = None
        self.last_discrete: tuple | None = None
        self.last_logged: dict[str, Any] = {}

        # Throttle bits latched at startup, so "since I started watching" is
        # separate from "since boot" - the since-boot bits survive a soft
        # reboot and will have you re-debugging yesterday's brownout.
        self.baseline_throttled: int | None = None
        self.sticky_since_start = 0
        self.service_cache: dict[str, bool | None] = {}
        self.restart_cache: int | None = None
        self.restarts_warned = -1
        self.sample_count = 0
        self.heartbeat = 0
        self.running = True

    def log_file_for_this_boot(self) -> Path | None:
        """
        One file per boot, not per day and not per start. A restart inside the
        same boot appends to the file already open for it, so a crash loop
        leaves one readable file instead of a directory full of fragments.

        boot_count is in the name because the clock cannot be trusted: a Pi
        with no RTC battery comes up with the wrong time until something syncs
        it, so two boots can otherwise claim the same timestamp.
        """
        if self.log_dir is None:
            return None
        # Boot number leads, zero-padded, so sorting by name is boot order.
        # Sorting by the date would trust the clock, and the clock is the thing
        # we cannot trust - one boot with a wrong year would sort to the end and
        # be mistaken for the most recent.
        # Which file this boot is using is recorded in boot_state.json, not
        # inferred from filenames. Globbing for the boot number looked right
        # until anything reset the counter, at which point boot 1 found boot
        # 1's file from a previous life and appended to it.
        if self.state.log_file:
            existing = Path(self.state.log_file)
            if existing.exists():
                return existing

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.log_dir / f"health_b{self.state.boot_count:05d}_{stamp}.jsonl"
        self.state.set_log_file(path)
        return path

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

        if self.sample_count % int(self.services["check_every_n"]) == 1 or not self.service_cache:
            for name in self.services["watch"]:
                self.service_cache[name] = service_active(name)
            self.restart_cache = systemd_restarts()

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
            # unclean_boots is just power cycles on a robot. This is the count
            # that means something.
            "brownout_boots": self.state.brownout_boots,
            "starts_this_boot": self.state.starts_this_boot,
            # starts_this_boot includes your deploys. This one is only what
            # systemd had to restart, which is the actual loop signal.
            "restarts_by_systemd": self.restart_cache,
            "last_boot_unclean": self.state.last_boot_was_unclean,
            # False means the card is refusing writes. Only the journal would
            # have it otherwise, and nobody reads the journal mid-match.
            "log_write_ok": self.log_write_ok,
        }
        s.update(read_supply())
        s.update(self.nt.status())
        for name, active in self.service_cache.items():
            s[f"svc_{name.replace('-', '_')}_active"] = active

        n = s.get("restarts_by_systemd")
        if (n is not None and n >= RESTART_LOOP_WARN and n > self.restarts_warned):
            self.restarts_warned = n
            LOG.warning("systemd has restarted this service %d times - it keeps "
                        "dying on its own. StartLimitIntervalSec=0 means systemd "
                        "will never stop trying, so nothing else will flag this. "
                        "Check: journalctl -u health_monitor -b", n)

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
            not s.get("usb_over_current"),
        ]
        checks += [v is not False for k, v in s.items() if k.startswith("svc_")]
        return all(checks)

    def prealarm(self, s: dict[str, Any]) -> bool:
        """
        Voltage close to the limit but not past it yet. Worth forcing a flush:
        if it keeps falling we want the run-up already on the card.
        """
        v = s.get("volts_in")
        limit = self.thresholds.get("volts_prealarm")
        return v is not None and limit is not None and v <= float(limit)

    def report_previous_boot(self) -> None:
        """After an unclean boot, say when it happened and what it looked like."""
        if not self.state.last_boot_was_unclean or self.log_dir is None:
            return
        records = read_last_records(self.log_dir, exclude=self.log_path)
        if not records:
            LOG.warning("previous boot ended uncleanly, but no log records "
                        "survived to say what happened")
            return

        def when(rec):
            try:
                return datetime.fromtimestamp(rec["ts"]).strftime("%H:%M:%S")
            except (KeyError, ValueError, OSError):
                return "??:??:??"

        LOG.info("previous boot ended without a clean shutdown - last record at "
                 "%s (clock is only trustworthy if the Pi has an RTC battery or "
                 "reached a time server)", when(records[-1]))
        for r in records:
            LOG.warning("  %s vin=%s temp=%s load=%s ok=%s", when(r),
                        fmt(r.get("volts_in"), "V", 2),
                        fmt(r.get("temp_c"), "C"),
                        fmt(r.get("load1"), "", 2),
                        r.get("ok"))

        # Distinguishing a brownout from someone opening the main breaker.
        #
        # "Unclean" on its own means nothing on a robot: cutting the breaker
        # never sends SIGTERM, so EVERY boot is unclean. What separates a fault
        # from a normal power-off is what the Pi was doing just before it went.
        #
        # Undervoltage is the strong signal - the firmware latches those bits,
        # and opening a breaker does not set them. A falling voltage trend is
        # the weaker backup for a sag too brief to have tripped a bit.
        volts = [r["volts_in"] for r in records if r.get("volts_in") is not None]
        undervolted = any(r.get("undervoltage_since_start")
                          or r.get("undervoltage_now")
                          or r.get("throttled_since_start") for r in records)
        # "Falling" needs a threshold. volts[-1] < volts[0] was true for a
        # tenth of a millivolt of noise, so a perfectly clean power-off had
        # about even odds of being called a brownout. The readings print to two
        # decimals, so 5.168 -> 5.161 looked flat and still tripped it.
        #
        # A real sag either drops further than the noise band, or ends below
        # the pre-alarm line.
        drop = (volts[0] - volts[-1]) if len(volts) >= 2 else 0.0
        sagging = (drop >= float(self.deltas["volts"])
                   or (volts and volts[-1] <= float(self.thresholds["volts_prealarm"])))

        if undervolted or sagging:
            self.state.record_brownout()
            LOG.error("  BROWNOUT: %s before power was lost. This was not a normal "
                      "power-off. brownout_boots=%d",
                      "undervoltage was flagged" if undervolted
                      else "voltage was falling",
                      self.state.brownout_boots)
        elif volts:
            LOG.info("  voltage was steady right up to the cut - reads as a normal "
                     "power-off, not a fault")

    # -- change detection -------------------------------------------------

    def discrete_signature(self, s: dict[str, Any]) -> tuple:
        """The fields that either changed or didn't - no tolerance involved."""
        booleans = tuple(sorted(
            (k, v) for k, v in s.items()
            if isinstance(v, bool) and k not in ("last_boot_unclean",)
        ))
        return (booleans, s.get("throttled_raw"), s.get("restarts_by_systemd"))

    def changed(self, s: dict[str, Any]) -> bool:
        """
        True when this sample differs meaningfully from the last one LOGGED.

        Continuous values must move at least their delta away from the last
        value written down. Note "from the last value logged" - rounding
        readings into fixed buckets and watching the bucket number looks
        equivalent but isn't, because a reading parked on a bucket boundary
        flips across it on noise alone and logs every sample.
        """
        if self.last_discrete is None:
            return True
        if self.discrete_signature(s) != self.last_discrete:
            return True
        return bool(self.moved(s))

    def moved(self, s: dict[str, Any]) -> set[str]:
        """
        Which continuous fields have moved their delta since the last log.

        log_sample() uppercases these, so a glance at a line tells you what
        actually changed instead of leaving you to diff it against the one
        above.
        """
        out = set()
        for field, delta_key in DELTA_FIELDS.items():
            current = s.get(field)
            previous = self.last_logged.get(field)
            if current is None and previous is None:
                continue
            if current is None or previous is None:
                out.add(field)   # a reading appeared or vanished; both matter
            elif abs(current - previous) >= float(self.deltas[delta_key]):
                out.add(field)
        return out

    def remember(self, s: dict[str, Any]) -> None:
        """Record what we just logged, so the next comparison is against it."""
        self.last_discrete = self.discrete_signature(s)
        for field in DELTA_FIELDS:
            self.last_logged[field] = s.get(field)
        self.last_logged["throttled_raw"] = s.get("throttled_raw")

    def log_sample(self, s: dict[str, Any], reason: str) -> None:
        """Log the inputs to the verdict, never just the verdict."""
        raw = s["throttled_raw"]
        moved = self.moved(s) if self.last_discrete is not None else set()
        thr_moved = raw != (self.last_logged.get("throttled_raw", raw))

        def tag(label: str, field: str) -> str:
            """UPPERCASE the label when that reading moved. Plain text, so it
            survives grep, the journal and the .jsonl - unlike colour codes."""
            return label.upper() if field in moved else label

        parts = [
            "OK" if s["ok"] else "BAD",
            ("THR" if thr_moved else "thr") +
            (f"=0x{raw:x}" if raw is not None else "=n/a"),
            f"{tag('temp', 'temp_c')}={fmt(s['temp_c'], 'C')}",
            f"{tag('vin', 'volts_in')}={fmt(s['volts_in'], 'V', 2)}",
            # "draw", not "pwr": this is consumption, and pwr read as though
            # it were the supply's capacity.
            f"{tag('draw', 'power_w')}={fmt(s['power_w'], 'W')}",
            f"cpu={fmt(s['cpu_mhz'], '', 0)}",
            f"{tag('mem', 'mem_available_mb')}={fmt(s['mem_available_mb'], '', 0)}",
            f"{tag('disk', 'disk_free_mb')}={fmt(s['disk_free_mb'], '', 0)}",
            f"load={fmt(s['load1'], '', 2)}",
            f"up={fmt(s['uptime_s'], '', 0)}",
            f"boots={s['boot_count']}/{s['unclean_boots']}",
        ]
        # up / DOWN / ? rather than svc_photonvision_active=None
        for k, v in s.items():
            if k.startswith("svc_"):
                name = k[4:].removesuffix("_active")
                parts.append(f"{name}=" + ("up" if v else "?" if v is None else "DOWN"))

        line = f"[{reason}] " + " ".join(parts)

        # The decoded text is 50-odd characters of mostly unchanging history.
        # Print it when the word changes, not on every line.
        if raw is not None and raw != self.last_described:
            self.last_described = raw
            LOG.info("throttle word is now %s", describe_throttled(raw))

        # last_boot_unclean used to raise the level here, which meant one
        # unclean boot turned EVERY line of that entire boot into a WARNING -
        # and on a robot every boot is unclean, so nothing would ever be INFO
        # again. The severity signal died. It is reported once at startup by
        # report_previous_boot(), which is where it belongs.
        t = self.thresholds
        if not s["ok"]:
            LOG.error(line)
        elif (s.get("temp_c") or 0) >= t["temp_warn_c"]:
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
        if not force and (now - self.last_flush) < float(self.storage["flush_interval_s"]):
            return
        self.last_flush = now    # set before the attempt, so a failing card
                                 # backs off to the interval instead of being
                                 # retried on every single sample
        path = self.log_path

        # Snapshot rather than drain: nothing leaves the buffer until it is
        # actually on the card.
        records = list(self.buffer)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                for record in records:
                    f.write(json.dumps(record, default=str) + "\n")
                f.flush()              # Python buffer -> kernel
                os.fsync(f.fileno())   # kernel page cache -> the actual card
                # Without the fsync, closing the file only reaches the page
                # cache, which writes back tens of seconds later - and a power
                # cut takes whatever is still sitting there.
        except OSError as e:
            # Keep the records and retry. maxlen already bounds the buffer,
            # so there is no need to throw evidence away here.
            self.log_write_ok = False
            LOG.error("could not flush health log to %s: %s "
                      "(%d record(s) still buffered, will retry)",
                      path, e, len(records))
            return
        self.log_write_ok = True
        for _ in records:
            self.buffer.popleft()

    # -- main loop --------------------------------------------------------

    def stop(self, signum=None, frame=None) -> None:
        LOG.info("stopping on signal %s — marking clean shutdown", signum)
        self.running = False

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        interval = float(self.cfg["interval_s"])
        heartbeat_log_s = float(self.storage["heartbeat_log_s"])
        LOG.info("health_monitor starting: interval=%.2fs table=%s state=%s",
                 interval, self.cfg["nt"]["table"], self.storage["state_dir"])
        LOG.info("writing records to %s", self.log_path or "(disk logging disabled)")
        supply = read_supply()
        ma = supply["supply_max_current_ma"]
        if ma is None:
            pass                          # not a Pi
        elif ma >= 5000:
            LOG.info("supply negotiated %dmA - full 5A profile, USB peripherals "
                     "get 1.6A", ma)
        else:
            LOG.warning("supply negotiated only %dmA. The board will run, but USB "
                        "peripherals are capped at 600mA TOTAL instead of 1.6A. "
                        "A 27W charger is usually 27W at 9V or 12V; the Pi needs "
                        "5V at 5A specifically.", ma)

        self.report_previous_boot()

        # systemd holds the service in "activating" until this arrives, then
        # starts enforcing WatchdogSec.
        if sd_notify("READY=1"):
            LOG.info("systemd watchdog active")

        next_t = time.monotonic()
        while self.running:
            try:
                s = self.sample()
                self.nt.publish(s)

                now = time.monotonic()
                if self.changed(s):
                    self.log_sample(s, "change" if self.last_discrete is not None else "baseline")
                    self.buffer_record(s)
                    self.remember(s)
                    self.last_heartbeat_log = now
                    if not s["ok"] or self.prealarm(s):
                        self.flush(force=True)  # get the evidence onto disk now
                elif (now - self.last_heartbeat_log) >= heartbeat_log_s:
                    self.log_sample(s, "heartbeat")
                    self.buffer_record(s)
                    self.last_heartbeat_log = now
                    if not s["ok"] or self.prealarm(s):
                        # A state that goes bad and stays bad stops changing,
                        # so these heartbeats become the only ongoing record.
                        self.flush(force=True)

                self.flush()

                # Only reached if the whole iteration completed. A hang inside
                # it stops these, and systemd restarts us - which Restart=always
                # cannot do, because a stuck process never exits.
                sd_notify("WATCHDOG=1")
            except Exception:
                LOG.exception("sample failed — continuing")

            next_t += interval
            sleep = next_t - time.monotonic()
            if sleep < 0:                 # we fell behind; resync rather than spin
                next_t = time.monotonic()
                sleep = 0
            time.sleep(sleep)

        sd_notify("STOPPING=1")
        self.flush(force=True)
        self.state.mark_clean()
        self.nt.close()
        LOG.info("health_monitor stopped cleanly")
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
    ma = s["supply_max_current_ma"]
    print(f"supply negotiated  : {'n/a' if ma is None else str(ma) + ' mA'}"
          + ("" if ma is None or ma >= 5000 else "   <-- under 5000, USB capped at 600mA"))
    print(f"usb high current   : {s['usb_high_current']}")
    print(f"usb over current   : {s['usb_over_current']}")
    print(f"memory available   : {fmt(s['mem_available_mb'], ' MB', 0)}")
    print(f"disk free          : {fmt(s['disk_free_mb'], ' MB', 0)}")
    print(f"load (1m)          : {fmt(s['load1'], '', 2)}")
    print(f"uptime             : {fmt(s['uptime_s'], ' s', 0)}")
    print(f"nt connected       : {s['nt_connected']}  offset_us={s['nt_offset_us']}")
    print(f"log writes ok      : {s['log_write_ok']}")
    print(f"systemd restarts   : {s['restarts_by_systemd']}"
          + ("   <-- restart loop" if (s['restarts_by_systemd'] or 0) >= RESTART_LOOP_WARN
             else ""))
    print(f"starts this boot   : {s['starts_this_boot']}"
          + ("   <-- restart loop?" if s['starts_this_boot'] >= RESTART_LOOP_WARN else ""))
    print(f"brownout boots     : {s['brownout_boots']}"
          + ("   <-- previous boot ended in a sag" if s["last_boot_unclean"]
             and s["brownout_boots"] else ""))
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

    # systemd sets JOURNAL_STREAM when it is capturing our output. The journal
    # timestamps every line itself, so ours is a second timestamp on the same
    # line - and journalctl -o cat still shows it. Drop it under systemd, keep
    # it when running by hand where nothing else provides one.
    under_systemd = "JOURNAL_STREAM" in os.environ
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=("%(levelname)-7s %(message)s" if under_systemd
                else "%(asctime)s %(levelname)-7s %(message)s"),
        # stderr, not stdout: --json writes the sample to stdout and log lines
        # would corrupt it. systemd sends both to the journal either way.
        stream=sys.stderr,
    )

    cfg = load_config(args.config)
    if not args.verbose:
        level = str(cfg.get("log_level", "INFO")).upper()
        logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
    if args.interval:
        cfg = deep_merge(cfg, {"interval_s": args.interval})
    if args.once:
        cfg = deep_merge(cfg, {"nt": {"enabled": False}, "storage": {"log_dir": None}})

    mon = HealthMonitor(cfg, nt_enabled=not (args.no_nt or args.once),
                        read_only=args.once)

    if args.once:
        print_once(mon, args.json)
        return 0
    return mon.run()


if __name__ == "__main__":
    sys.exit(main())
