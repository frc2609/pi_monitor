# Health Monitor — vision coprocessor

A power and throttle watchdog for the Raspberry Pi 5 that runs our AprilTag vision.
It runs as a systemd service, samples the Pi's power and thermal state once a second,
and records what it sees so that **power problems are visibly distinguishable from
software bugs.**

That distinction is the entire point. FRC robots share one 12V battery between the
drivetrain motors and everything else. During hard acceleration the motors pull
hundreds of amps and battery voltage sags. A coprocessor that browns out mid-match
produces symptoms — frozen vision, dropped USB cameras, a corrupted filesystem —
that look *exactly* like a bug in your code. Without a witness, teams rewrite working
pipelines chasing a wiring fault.

This is the difference between "vision is flaky, let's rewrite it" and "vision browned
out at match time 47, fix the wiring."

## What this is not

**This repo does not do vision.** No tag detection, no pose solving, no calibration.
That is [PhotonVision](https://docs.photonvision.org/)'s job — a mature, open-source
FRC application with years of field testing behind it, configured through a web UI
rather than through code. We are deliberately not writing our own detector.

Our Python is limited to the things PhotonVision does not do. Right now that is
health monitoring, and nothing else.

## What lives where

Three separate concerns, and putting logic on the wrong side is the most common
mistake in a vision project:

| Concern | Runs on | Language | Where |
|---|---|---|---|
| Tag detection, pose solving, multi-tag | Pi | Config, not code | PhotonVision web UI |
| Camera calibration, pipeline tuning | Pi | Config, not code | PhotonVision web UI |
| Health and power monitoring | Pi | Python | **this repo** |
| Pose estimator, trust weighting, robot behaviour | roboRIO | Java | the robot code repo |

The Pi *produces measurements*. The roboRIO *decides what to do with them* — that
belongs in Java where it is testable in simulation and visible in match logs.

## Hardware target

| Item | Detail |
|---|---|
| Board | Raspberry Pi 5, 8GB |
| OS | Raspberry Pi OS (Debian trixie base), 64-bit |
| Boot | Console autologin, headless — SSH only, no desktop |
| Storage | microSD |
| Power | 5V/5A (27W) USB-C. **This matters more than anything else here.** |

> **Known issue right now:** the development Pi is running on a 900mA supply, far
> below what a Pi 5 wants, and `vcgencmd get_throttled` reports `0x50000`. A proper
> regulator is being sourced. See the `2026-08-21 — get_throttled returns 0x50000`
> entry in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Setup from a bare Pi

Assumes a Pi already imaged, on the network, and reachable over SSH.

```bash
# 1. System packages. PyYAML comes from apt on purpose - see "Python packaging".
sudo apt update
sudo apt install -y git python3-yaml

# 2. Clone. The path matters: the service unit expects ~/vision/monitor.
mkdir -p ~/vision
git clone <your-repo-url> ~/vision/monitor
cd ~/vision/monitor

# 3. Check it works before installing it as a service.
python3 src/health_monitor.py --once

# 4. Install and start the service.
./scripts/install-service.sh

# 5. Verify.
systemctl status health-monitor
journalctl -u health-monitor -f
```

Step 3 before step 4 is deliberate. If it does not work in the foreground where you
can see the error, it will not work any better hidden behind systemd.

### Running it by hand

```bash
python3 src/health_monitor.py --once           # one readable sample, then exit
python3 src/health_monitor.py --once --json    # same, machine-readable
python3 src/health_monitor.py --no-nt -v       # foreground, no NetworkTables, debug
```

`--once` never writes to the persistent boot record, so it is safe to run on a Pi that
already has the service installed without corrupting the boot history.

It also runs on a laptop with no `vcgencmd` present — every unavailable reading comes
back `n/a` instead of crashing. Develop on the laptop, deploy to the Pi.

## What it watches

| Reading | Source | Why it is here |
|---|---|---|
| Throttle / undervoltage flags | `vcgencmd get_throttled` | The single most useful number on the Pi |
| SoC temperature | `vcgencmd measure_temp` | Thermal throttling in a hot pit |
| Actual ARM clock | `vcgencmd measure_clock arm` | Proves capping is real, not just flagged |
| Rail volts and amps | `vcgencmd pmic_read_adc` | Real measurements, not just fault bits |
| Boot count, clean-shutdown marker | `/proc` plus a state file | A brownout reset otherwise looks like a normal boot |
| Memory, disk, load, uptime | `/proc`, `statvfs` | The boring failures that still end a match |
| PhotonVision service state | `systemctl is-active` | Is the thing we exist to witness actually running? |

### The one subtlety worth understanding

`get_throttled` returns a bitfield holding two *different kinds* of information:

| Bit | Meaning |
|---|---|
| 0 | Under-voltage **right now** |
| 1 | ARM frequency capped right now |
| 2 | Throttled right now |
| 3 | Soft temperature limit active right now |
| 16 | Under-voltage **has occurred** since boot |
| 17 | Frequency capping has occurred since boot |
| 18 | Throttling has occurred since boot |
| 19 | Soft temperature limit has occurred since boot |

Bits 0-3 are live state. Bits 16-19 are **sticky history that only clears on a full
power cycle** — a soft reboot will not reset them. They are not the same thing, and
the monitor keeps them separate.

The sticky bits mislead constantly. They will happily report yesterday's brownout as
though it just happened and send you off re-debugging a solved problem. So the monitor
also latches its own baseline at startup and separately reports
`undervoltage_since_start` and `throttled_since_start` — bits that appeared *after this
service began watching.* **During a match, that is the number that means something.**

## Logging

- **On change, not on every sample.** Continuous logging buries the signal and wears
  out the SD card. Continuous values are bucketed with a hysteresis band so sensor
  noise alone does not count as a change.
- **Heartbeat every 30 seconds** regardless, so silence means a dead process rather
  than a quiet one. This is deliberately well under match length — a heartbeat
  longer than a match means a clean match can produce no record at all, and you
  cannot tell a healthy quiet monitor from one that died before the match started.
- **Log the inputs to a decision, never just the verdict.** "Rejected" tells you
  nothing. "temp=81.0C exceeds crit 80.0" tells you what to do.
- Records buffer in RAM and flush to disk every 10 seconds — and immediately when
  anything fails the health check. Evidence you did not write down is not evidence,
  and a hard power cut is the event this service exists to witness: whatever is
  still in RAM when the power goes is gone. Ten seconds is roughly 7% of a match.

Everything also reaches the journal: `journalctl -u health-monitor -f`.

### NetworkTables

Health data publishes to `/Vision/health` as **numbers and booleans only.** Never
formatted strings — strings do not graph in AdvantageScope and cannot be consumed
without parsing.

Set `nt_mode` in [config/config.yaml](config/config.yaml): `server` on the bench when
no roboRIO is present, `client` on the robot.

## Python packaging

**Read this before installing anything.** It has already been decided.

- OpenCV comes from **apt** (`python3-opencv`, 4.10.x), not pip. The apt build includes
  the contrib modules, and `cv2.aruco` — needed for ChArUco calibration — lives in
  contrib. The plain PyPI `opencv-python` does not have it, and the `AttributeError`
  you get does not point at the cause.
- **Do not pip install any OpenCV package.** Not `opencv-python`, not
  `opencv-contrib-python`, not the headless variants.
- **Do not use `pip install --break-system-packages`.** Pi OS enforces PEP 668 and
  refuses system-wide pip installs. That guard is protecting you. Override it and you
  end up with two OpenCVs shadowing each other on `sys.path`, debugging against
  documentation for a version you are not running.
- PyPI offers OpenCV 5.x. We deliberately declined it — the FRC ecosystem
  (PhotonVision, robotpy) is built against 4.x, and so is every tutorial you will read.

**This project uses a venv anyway, for exactly one reason:** NetworkTables (`pyntcore`)
is pip-only, because robotpy is not packaged in Debian. `scripts/install-service.sh`
creates it with `--system-site-packages` so apt's packages stay visible rather than a
second copy being installed alongside them:

```bash
python3 -m venv --system-site-packages .venv
```

If OpenCV ever behaves strangely, the first thing to check is which build is loaded:

```bash
python3 -c "import cv2; print(cv2.__version__); print(cv2.__file__)"
```

A path under `dist-packages` means apt. Under `site-packages` means pip. If both are
installed, Python silently takes whichever comes first on `sys.path`.

## Service management

```bash
sudo systemctl status health-monitor     # is it running?
journalctl -u health-monitor -f          # live logs
journalctl -u health-monitor -b          # everything since this boot
sudo systemctl restart health-monitor    # after a config change
```

**`start` and `enable` are different things.** `start` runs it now. `enable` makes it
come back after a reboot. `enable --now` does both, which is what the install script
uses. A service that was started but never enabled works perfectly until the first
power cycle, and then silently does not — a classic competition-day failure.

The monitor is a **separate service from PhotonVision, on purpose.** It is not ordered
against it and not bound to it. If a brownout kills PhotonVision, the monitor has to
survive to record why. A witness that dies alongside the thing it is watching is useless.

## When something is weird

Start here, before believing anything else:

```bash
vcgencmd get_throttled     # 0x0 is the only clean answer
vcgencmd measure_temp
free -h
systemctl status photonvision
journalctl -u health-monitor -b
```

Then read **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — a running log of
problems this team has actually hit and how they were solved. **Add to it when you
solve something.** The entry nobody writes is the week somebody else loses.

## Layout

```
monitor/
├── README.md
├── requirements.txt
├── config/
│   └── config.yaml              team number, intervals, thresholds
├── docs/
│   └── TROUBLESHOOTING.md       running log - append to this
├── scripts/
│   └── install-service.sh       idempotent; safe to re-run
├── services/
│   └── health-monitor.service   systemd unit
└── src/
    └── health_monitor.py        the monitor itself
```

Nothing here is prefixed with the team number. The team number appears in exactly one
place: `config/config.yaml`, as a value. Code that is not team-specific should not look
like it is.
