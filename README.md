# Health Monitor

A watchdog for the Raspberry Pi 5. It runs as a systemd
service and records the Pi's power and thermal state so we can tell power problems
apart from software bugs.

The robot runs everything off one battery. When the drivetrain and other systems pull hard, voltage
sags. If the Pi's 5V supply sags with it you get frozen vision, USB cameras dropping
out, corrupted files, and random reboots.

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

Raspberry Pi 5 8GB, headless over SSH.

Power is 5V/5A (27W). Two separate things go wrong below that.

The firmware caps all USB peripherals at 600mA combined unless it negotiates a 5A
supply, in which case it allows 1.6A. That becomes a problem as soon as you run more
than one camera.

And if the supply can't hold 5V under load, the Pi throttles the CPU to survive. That
is the `0x50000` this monitor exists to catch, and it looks like a software bug when
you hit it.
