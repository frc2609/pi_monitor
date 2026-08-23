# Health Monitor

A watchdog for the Raspberry Pi 5. It runs as a systemd
service and records the Pi's power and thermal state so we can tell power problems
apart from software bugs.

The robot runs everything off one battery. When the drivetrain and other systems pull hard, voltage
sags. If the Pi's 5V supply sags with it you get frozen vision, USB cameras dropping
out, corrupted files, and random reboots.

## Docs

Not in this repo yet. `docs/DESIGN.md` and `docs/RUNNING.md` are written but held back
until the install has been walked through on a real Pi, and the troubleshooting log
lives one level up in the shared docs because it covers the whole Pi rather than this
service.

- `docs/RUNNING.md`: how to run it and read its output. Bench, robot, and
  AdvantageScope with or without a roboRIO.
- `docs/DESIGN.md`: how it works and why it was built this way.
- `../docs/TROUBLESHOOTING.md`: log of problems we've actually hit. Add to it when you
  solve something.

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

Power is 5V/5A (27W), and it matters more than anything else here. If the Pi doesn't
negotiate a 5A supply over USB-PD, the firmware caps all USB peripherals at 600mA
combined. It raises that to 1.6A once it does. A single camera draws roughly 500mA and
we expect to run more than one, so an undersized supply doesn't just throttle the CPU,
it starves the cameras.

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
