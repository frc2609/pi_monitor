# Health Monitor — vision coprocessor

A power and throttle watchdog for the Raspberry Pi 5 that runs our AprilTag vision.
It runs as a systemd service and records what the Pi's power and thermal state was
doing, so that **power problems are visibly distinguishable from software bugs.**

An FRC robot runs everything off one battery. When the drivetrain pulls hundreds of
amps, voltage sags — and a coprocessor that browns out produces frozen vision, dropped
USB cameras and a corrupted filesystem. None of those say "power" anywhere. Without a
witness, teams rewrite working code chasing a wiring fault.

This is the difference between *"vision is flaky, let's rewrite it"* and *"vision
browned out at 14:32:07, fix the wiring."*

## Start here

| Doc | What's in it |
|---|---|
| **[docs/RUNNING.md](docs/RUNNING.md)** | How to run it, and how to read its output — bench, robot, and AdvantageScope with or without a roboRIO |
| **[docs/DESIGN.md](docs/DESIGN.md)** | How it works and why each decision was made |
| **[../docs/TROUBLESHOOTING.md](../docs/TROUBLESHOOTING.md)** | Running log of problems this team has actually hit. **Append to it.** |

## What this is not

**This repo does not do vision.** No tag detection, no pose solving, no calibration.
That is [PhotonVision](https://docs.photonvision.org/)'s job — a mature, open-source
FRC application with years of field testing behind it, configured through a web UI
rather than code. We are deliberately not writing our own detector.

Our Python is limited to what PhotonVision doesn't do. Right now that is health
monitoring, and nothing else.

| Concern | Runs on | Language | Where |
|---|---|---|---|
| Tag detection, pose solving, calibration | Pi | Config, not code | PhotonVision web UI |
| Health and power monitoring | Pi | Python | **this repo** |
| Pose estimator, trust weighting, robot behaviour | roboRIO | Java | the robot code repo |

The Pi *produces measurements*. The roboRIO *decides what to do with them* — that
belongs in Java where it's testable in simulation.

## Hardware

| Item | Detail |
|---|---|
| Board | Raspberry Pi 5, 8GB |
| OS | Raspberry Pi OS (Debian trixie base), 64-bit |
| Boot | Headless — SSH only, no desktop |
| Power | 5V/5A (27W). **This matters more than anything else here.** |

> **Known issue:** the dev Pi is on a 900mA supply and `vcgencmd get_throttled` reports
> `0x50000`. A regulator is being sourced — the compared options, including a commonly
> recommended part that is **not** rated high enough for a Pi 5, are in the `0x50000`
> entry of [../docs/TROUBLESHOOTING.md](../docs/TROUBLESHOOTING.md).

## Quick start

```bash
sudo apt update && sudo apt install -y git python3-yaml
mkdir -p ~/vision && git clone <your-repo-url> ~/vision/monitor
cd ~/vision/monitor

python3 src/health_monitor.py --once      # check it works first
./scripts/install-service.sh              # then install it

journalctl -u health-monitor -f
```

Full detail in [docs/RUNNING.md](docs/RUNNING.md).

## Python packaging — do not modernise this

OpenCV comes from **apt** (`python3-opencv`, 4.10.x), never pip — apt's build includes
the contrib modules that ChArUco calibration needs. Never
`pip install --break-system-packages`; PEP 668 is protecting you from two OpenCVs
shadowing each other on `sys.path`.

A venv exists for exactly one reason: `pyntcore` is pip-only. It's created with
`--system-site-packages` so apt's packages stay visible. `install-service.sh` handles
it.

Full reasoning in [../docs/operating-standards.md](../docs/operating-standards.md) §4.

## Layout

```
monitor/
├── README.md
├── requirements.txt
├── config/config.yaml               everything tunable, commented
├── docs/
│   ├── DESIGN.md                    how and why
│   └── RUNNING.md                   how to run and read it
├── scripts/install-service.sh       idempotent installer
├── services/health-monitor.service  systemd unit
├── src/health_monitor.py
└── tests/                           three files, all run without a Pi
```

The troubleshooting log lives at `../docs/`, one level up, because it covers the whole
Pi platform rather than this monitor alone. A Pi that clones only this repo does not
get it.

Nothing here is prefixed with the team number. It appears in exactly one place:
`config/config.yaml`, as a value.
