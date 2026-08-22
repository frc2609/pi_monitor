# Running it, and reading what it says

*For why it works the way it does, see [DESIGN.md](DESIGN.md).*

---

## Four ways to run it

| Way | Command | Use it when |
|---|---|---|
| **One sample** | `python3 src/health_monitor.py --once` | "Is this Pi healthy right now?" |
| **One sample, machine-readable** | `python3 src/health_monitor.py --once --json` | Piping into `jq` or a script |
| **Foreground** | `python3 src/health_monitor.py --no-nt -v` | Developing, or watching it live while you break something |
| **As a service** | `./scripts/install-service.sh` | Always, on any Pi that matters |

`--once` is safe on a Pi with the service already running. It never writes the boot
record and never touches the log file.

Everything runs on a laptop with no `vcgencmd` — readings just come back `n/a`.

---

## First install, from a clone

```bash
sudo apt update
sudo apt install -y git python3-yaml

mkdir -p ~/vision
git clone <your-repo-url> ~/vision/monitor
cd ~/vision/monitor

python3 src/health_monitor.py --once      # check it works BEFORE hiding it in systemd
./scripts/install-service.sh
```

The installer creates the venv, installs `pyntcore`, runs one foreground sample as a
smoke test, makes the journal persistent, installs the unit, and confirms the service
is **enabled** — not just started.

> **`start` and `enable` are different.** `start` runs it now. `enable` makes it come
> back after a reboot. A service that was started but never enabled works perfectly
> until the first power cycle, and that power cycle is usually at a competition.

---

## Where the output goes

Three places, same data, different strengths.

| Destination | Strength | Weakness |
|---|---|---|
| **journal** | always there, human readable | text, not graphable |
| **`.jsonl` file** | survives a power cut, full fidelity including `null` | after the fact only |
| **NetworkTables** | live, graphable, overlays with match data | needs a network, and a RIO to be logged |

**Use NT for analysis. Use the file for forensics.** The file exists when NT can't:
before the network is up, on a bench with no roboRIO, and — most importantly — it's
what the *next boot* reads back to tell you what killed the last one.

---

## Reading the journal

```bash
journalctl -u health-monitor -f          # live
journalctl -u health-monitor -b          # since this boot
journalctl -u health-monitor -b -1       # the PREVIOUS boot - after a power cut
journalctl -u health-monitor -p warning  # just the bad news
```

A line looks like this:

```
[change] ok=False throttled=0x50005 (under-voltage RIGHT NOW; throttled now;
under-voltage has occurred; throttling has occurred) temp=59.0°C vin=4.72V
pwr=6.8W cpu=1500MHz mem=7100MB disk=24000MB load=1.20 up=312s
boots=1/0unclean svc_photonvision_active=True
```

Reading it:

- **`[baseline]`** first sample, **`[change]`** something moved, **`[heartbeat]`**
  nothing moved but it's still alive
- **INFO → ERROR** the moment `ok` flips
- The throttle word is decoded in English, and **"RIGHT NOW" vs "has occurred"**
  separates live from sticky history
- **`cpu=1500MHz`** next to a falling `vin` is throttling actually biting — the flag
  and its consequence side by side

---

## Reading the file

`/var/log/health-monitor/health-b00004-20260822-183229.jsonl` — one JSON object per
line, one file per boot.

```bash
ls -t /var/log/health-monitor/                # newest boot last by name, by boot number
tail -f /var/log/health-monitor/health-b*.jsonl

# every fault
jq -c 'select(.ok == false) | [.ts, .volts_in, .throttled_raw]' health-b*.jsonl

# the voltage trace, and whether the clock dropped with it
jq -c '[.ts, .volts_in, .cpu_mhz]' health-b*.jsonl

# worst voltage this boot
jq -s 'map(.volts_in | select(. != null)) | min' health-b*.jsonl

# did it ever throttle while this service was watching?
jq -c 'select(.throttled_since_start == true) | .ts' health-b*.jsonl
```

**`null` means the reading was unavailable**, not zero. That distinction is preserved
in the file and lost on NetworkTables (where it becomes NaN).

---

## Reading it in AdvantageScope

### On the bench, no roboRIO

The Pi hosts NetworkTables itself.

```yaml
# config/config.yaml
nt_mode: server
nt_server: null
```

Then in AdvantageScope: **File → Connect to Live Source**, enter the Pi's address
(`vision-pi.local` or its static IP). Health topics appear under `/Vision/health`.

This gives you live graphs with no robot at all. Good for watching a voltage sag while
you load the CPU deliberately.

> OutlineViewer works too and is quicker for just checking values are arriving.

### On the bench, with a laptop NT server

Useful when you want robot code in simulation and the Pi feeding it real data.

1. Start the NT server on your laptop (simulation does this, or OutlineViewer can)
2. Point the Pi at it:

```yaml
nt_mode: client
nt_server: 10.0.0.42        # your laptop's address
```

3. AdvantageScope connects to the laptop, and sees Pi data and simulated robot data
   together

### On the robot, with a roboRIO — the one that matters

```yaml
nt_mode: client
nt_server: null             # null means "use the team number"
team: 2609
```

**Then make the RIO log it.** In robot code:

```java
DataLogManager.start();     // NT logging is on by default
```

That records every NT change — including everything the Pi publishes — into the
`.wpilog` on the RIO's USB stick, **on the same timebase as match data**.

Afterwards: pull the `.wpilog`, open it in AdvantageScope, and drag
`/Vision/health/volts_in` onto the same graph as battery voltage, match time, and
drivetrain current.

**No clock syncing is needed.** NT4 synchronises the client's clock to the server's as
part of the protocol, so the Pi's values are already aligned. `nt_offset_us` in the
health data shows that offset if you want to confirm sync is really happening.

> **This is how you answer "why did the voltage drop."** The Pi only sees its own 5V
> side. Put it next to the battery and the drivetrain current in one timeline and the
> cause is usually obvious.

### With no NetworkTables at all

Read the `.jsonl`. Everything is in there, plus the `null`s that NT can't express.

---

## What to look at when something's wrong

**In order.** Don't skip to step 3.

```bash
vcgencmd get_throttled          # 0x0 is the only clean answer
systemctl status health-monitor
journalctl -u health-monitor -b
```

Then, in the data:

1. **`ok`** — false means a threshold was crossed
2. **`throttled_since_start`** — did the rail sag *while this service was watching*
3. **`unclean_boots` / `last_boot_unclean`** — did it lose power
4. **`starts_this_boot`** — above 1 means something keeps killing the service; past 5
   it warns on its own
5. **`log_write_ok`** — false means the card is refusing writes and you're only seeing
   the journal
6. **`svc_photonvision_active`** — is the thing we exist to witness even running

After an unclean boot the service prints its own verdict on startup — when the last
record was written, the final few readings, and whether the voltage was falling
(brownout) or steady (something disconnected it).

**Then write it down.** [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md)
is the running log — newest first, copy the template. The entry nobody writes is the
week somebody else loses.

---

## Service management

```bash
sudo systemctl status health-monitor
sudo systemctl restart health-monitor     # after a config change
journalctl -u health-monitor -f
```

Re-running `./scripts/install-service.sh` is the normal way to deploy a changed unit
file — it's idempotent.

**Confirm the watchdog is armed** on a new install:

```bash
systemctl show health-monitor -p WatchdogUSec    # expect 60000000
journalctl -u health-monitor -b | grep "watchdog active"
```

---

## Tests

All three run anywhere. No Pi, no roboRIO, no network.

```bash
python3 tests/test_change_detection.py   # noise doesn't log, real movement does
python3 tests/test_boot_report.py        # unclean-boot reporting, crash-loop files
python3 tests/test_nt_publish.py         # NaN for missing readings, types
```

---

## Changing settings

Everything tunable is in [`config/config.yaml`](../config/config.yaml), commented.
After editing:

```bash
sudo systemctl restart health-monitor
```

Two rules worth keeping:

- **Size intervals against match length**, not card wear. A match is under three
  minutes; anything longer than that hides inside one.
- **`state_dir` and `log_dir` must match `StateDirectory=` and `LogsDirectory=`** in
  the unit file. systemd creates those directories, which is why the monitor never
  needs root.
