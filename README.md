# Health Monitor

A watchdog for the Raspberry Pi 5. It runs as a systemd
service and records the Pi's power and thermal state so we can tell power problems
apart from software bugs.

The robot runs everything off one battery. When the drivetrain and other systems pull hard, voltage
sags. If the Pi's 5V supply sags with it you get frozen vision, USB cameras dropping
out, corrupted files, and random reboots.

## Docs

- [docs/RUNNING.md](docs/RUNNING.md): how to run it and read its output. Covers the
  bench, the robot, and AdvantageScope with or without a roboRIO.
- [docs/DESIGN.md](docs/DESIGN.md): how it works and why it was built this way.
- [../docs/TROUBLESHOOTING.md](../docs/TROUBLESHOOTING.md): log of problems we've
  actually hit. Add to it when you solve something.

## Scope

No detection, no pose solving, no calibration. That's all
[PhotonVision](https://docs.photonvision.org/), which is configured through its web UI
rather than by writing code. We're not writing our own detector.

Our Python only covers what PhotonVision doesn't do, which right now is health
monitoring.

| Job                                              | Runs on | Written in          | Lives in        |
| ------------------------------------------------ | ------- | ------------------- | --------------- |
| Detection, pose solving, calibration             | Pi      | PhotonVision config | its web UI      |
| Health and power monitoring                      | Pi      | Python              | this repo       |
| Pose estimator, trust weighting, robot behaviour | roboRIO | Java                | robot code repo |

The Pi produces measurements. The roboRIO decides what to do with them, because that's
where you can test the decision in simulation.

## Hardware

Raspberry Pi 5 8GB, Raspberry Pi OS 64-bit (trixie), headless over SSH.

Power is 5V/5A (27W), and it matters more than anything else here. If the Pi does not detect 5A it will limit peripherals to 600mA total across all connected. A single camera needs ~500mA, we most likely will be running multiple,

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
