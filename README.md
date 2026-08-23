# Health Monitor

A watchdog for the Raspberry Pi 5 that runs our AprilTag vision. It runs as a systemd
service and records the Pi's power and thermal state so we can tell power problems
apart from software bugs.

The robot runs everything off one battery. When the drivetrain pulls hard, voltage
sags. If the Pi's 5V supply sags with it you get frozen vision, USB cameras dropping
out, corrupted files, and random reboots. None of that looks like a power problem when
you're staring at it, so teams end up rewriting working code to fix a wiring fault.

## Docs

- [docs/RUNNING.md](docs/RUNNING.md) — how to run it and read its output. Covers the
  bench, the robot, and AdvantageScope with or without a roboRIO.
- [docs/DESIGN.md](docs/DESIGN.md) — how it works and why it was built this way.
- [../docs/TROUBLESHOOTING.md](../docs/TROUBLESHOOTING.md) — log of problems we've
  actually hit. Add to it when you solve something.

## Scope

No detection, no pose solving, no calibration. That's all
[PhotonVision](https://docs.photonvision.org/), which is configured through its web UI
rather than by writing code. We're not writing our own detector.

Our Python only covers what PhotonVision doesn't do, which right now is health
monitoring.

| Job | Runs on | Written in | Lives in |
|---|---|---|---|
| Detection, pose solving, calibration | Pi | PhotonVision config | its web UI |
| Health and power monitoring | Pi | Python | this repo |
| Pose estimator, trust weighting, robot behaviour | roboRIO | Java | robot code repo |

The Pi produces measurements. The roboRIO decides what to do with them, because that's
where you can test the decision in simulation.

## Hardware

Raspberry Pi 5 8GB, Raspberry Pi OS 64-bit (trixie), headless over SSH.

Power is 5V/5A (27W), and it matters more than anything else here. The dev Pi is
currently on a 900mA supply and reports `throttled=0x50000`. We're sourcing a proper
regulator. Options and ratings are in the `0x50000` entry of
[../docs/TROUBLESHOOTING.md](../docs/TROUBLESHOOTING.md), including one commonly
recommended part that isn't rated high enough for a Pi 5.

## Setup

```bash
sudo apt update && sudo apt install -y git python3-yaml
mkdir -p ~/vision && git clone <your-repo-url> ~/vision/monitor
cd ~/vision/monitor

python3 src/health_monitor.py --once      # check it works first
./scripts/install-service.sh              # then install it

journalctl -u health-monitor -f
```

The installer figures out your username and repo path, so it works whether you're
`pi`, `admin`, or anything else. More detail in [docs/RUNNING.md](docs/RUNNING.md).

## Python packaging

Don't change this without reading
[../docs/operating-standards.md](../docs/operating-standards.md) §4 first.

OpenCV comes from apt (`python3-opencv`, 4.10.x), not pip. The apt build includes the
contrib modules, and ChArUco calibration needs them. Don't use
`pip install --break-system-packages`; PEP 668 is stopping you from ending up with two
OpenCVs on `sys.path`.

There is a venv, but only because `pyntcore` is pip-only. It's created with
`--system-site-packages` so apt's packages stay visible. The installer handles it.

## Layout

```
monitor/
├── config/config.yaml               everything tunable, commented
├── docs/
│   ├── DESIGN.md
│   └── RUNNING.md
├── scripts/install-service.sh       idempotent
├── services/health-monitor.service
├── src/health_monitor.py
└── tests/                           three files, none need a Pi
```

The troubleshooting log is one level up in `../docs/` because it covers the whole Pi,
not just this service. A Pi that clones only this repo won't get it.

Nothing here is named after the team number. It appears once, as a value in
`config/config.yaml`.
