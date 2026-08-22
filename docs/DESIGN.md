# Design — how the health monitor works, and why

*Why each part is the way it is. For running it and reading its output, see
[RUNNING.md](RUNNING.md).*

Values quoted here are the shipped defaults. **[`config/config.yaml`](../config/config.yaml)
is authoritative** — if the two disagree, the config is right and this file needs fixing.

---

## 1. The problem

An FRC robot runs everything off one 12V battery. During hard acceleration the
drivetrain pulls hundreds of amps and battery voltage sags. If the Pi's 5V supply
sags with it, you get:

- vision freezing
- USB cameras disappearing mid-match
- the filesystem corrupting
- the Pi silently rebooting

**None of those say "power" anywhere.** They look exactly like software bugs, and
teams rewrite working code chasing a wiring fault.

This monitor exists to make that distinction visible. It turns *"vision is flaky,
let's rewrite it"* into *"vision browned out at 14:32:07, fix the wiring."*

---

## 2. Why a separate service

The monitor runs as its own systemd unit, **not** as part of PhotonVision, and the
unit is deliberately **not** ordered against it:

```ini
After=network.target        # not network-online.target
# no After= or Requires= photonvision
```

Reasons:

- **A witness that dies with the thing it watches is useless.** If a brownout kills
  PhotonVision, the monitor has to survive to record why.
- **`network.target`, not `network-online.target`** — the monitor must start even
  when the network doesn't. A network that failed to come up is itself worth
  recording, and waiting on it would delay the one process guaranteed to be running
  when things break. NetworkTables reconnects on its own.
- **`StartLimitIntervalSec=0`** — systemd's default gives up after 5 restarts in 10
  seconds. That would retire the watchdog during exactly the brownout storm it was
  written for.

---

## 3. What it reads, and why each one

| Reading | Source | Why it earns its place |
|---|---|---|
| Throttle flags | `vcgencmd get_throttled` | The single most useful number on a Pi |
| Temperature | `vcgencmd measure_temp` | Thermal throttling in a hot pit looks like undervoltage |
| Actual ARM clock | `vcgencmd measure_clock arm` | Proves throttling is real, not just flagged |
| Rail volts and amps | `vcgencmd pmic_read_adc` | Real measurements, not just fault bits |
| Boot id, clean-shutdown marker | `/proc` + a state file | A brownout reset otherwise looks like a normal boot |
| Memory, disk, load, uptime | `/proc`, `statvfs` | The boring failures that still end a match |
| Service state | `systemctl is-active` | Is the thing we exist to witness actually running |

**Every reader returns `None` rather than raising.** A monitoring script must never
crash the thing it monitors, and must keep going when one sensor stops answering.
That is also what lets the whole thing run on a laptop with no `vcgencmd` at all.

**PMIC note:** `EXT5V` is the input rail and has no paired current channel, so total
draw is estimated by summing V×I over the rails that do pair up. It's an estimate,
not a wall-plug measurement — but it moves when the load moves, which is what spots
a supply that can't keep up.

---

## 4. The throttle bitfield — three different questions

`get_throttled` returns one hex word holding two *kinds* of information, and the
monitor derives a third.

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

- **Bits 0–3 are live state.** True only while it's happening.
- **Bits 16–19 are sticky history.** They survive a soft reboot and clear **only on a
  full power cycle**. They will happily report yesterday's brownout as though it just
  happened, and send you re-debugging a solved problem.
- **`*_since_start`** — the monitor latches the throttle word at startup as a baseline,
  then reports only bits that appeared *after it began watching*.

**During a match, `*_since_start` is the number that means something.** The other two
are context.

> A useful consequence: `throttled_raw` is part of change detection, so when the
> firmware latches a bit the very next sample logs it. A 50ms sag you never sampled
> the voltage for still gets timestamped to within a second. You lose the depth and
> shape, not the event.

---

## 5. Detecting power cuts — `boot_state.json`

The sticky bits **cannot** report the brownout that rebooted you, because a full
power cycle clears them. That's what the state file is for.

`/var/lib/health-monitor/boot_state.json` holds `boot_id`, `boot_count`,
`unclean_boots`, `starts_this_boot`, `clean_shutdown`.

| Event | What happens |
|---|---|
| Service starts | writes `clean_shutdown: false` |
| Clean stop (`systemctl stop`, `poweroff`) | SIGTERM → `mark_clean()` writes `true` |
| Power cut | no SIGTERM, so the file is left saying `false` |
| Next boot | kernel gives a new `boot_id`; a changed id **plus** an old record saying `false` means an unclean boot |

**Why `boot_id` rather than uptime** — it separates a real reboot from a service
restart inside the same boot. Same id means restart, and the boot count doesn't move.

**Why it's `fsync`ed before the rename** — the `clean=false` write has to reach the
card *before* power dies. An atomic rename over data still sitting in the page cache
misses the point of a file whose only job is surviving a power cut.

**`--once` never writes this file.** A one-shot inspection that wrote `false` and then
exited would make the next power cycle look like a brownout that never happened.

### What it reports afterwards

On the next start after an unclean boot, the monitor reads the tail of the previous
boot's log and says what it saw:

```
WARNING previous boot ended uncleanly - last record at 16:53:29
WARNING   16:53:27 vin=4.78V temp=59.0C load=1.40 ok=False
WARNING   16:53:28 vin=4.71V temp=60.1C load=1.40 ok=False
WARNING   16:53:29 vin=4.62V temp=61.0C load=1.40 ok=False
WARNING   voltage was falling before power was lost - reads as a brownout
          rather than something disconnecting it
```

Falling voltage into the cut reads as a brownout. Steady voltage right up to the cut
reads as something disconnecting power. That's the difference between a wiring
problem and someone knocking a plug out.

---

## 6. When it writes — change detection

Logging every sample would bury the signal and wear the card. A record is written
when:

1. **Something discrete changed** — any boolean, or the raw throttle word
2. **A continuous value moved** at least its `deltas` distance **from the last value
   actually logged**
3. **The heartbeat interval elapsed** (10s) even if nothing changed, so silence means
   a dead process rather than a quiet one

### Why "from the last value logged" matters

The obvious approach is to round readings into fixed buckets and watch the bucket
number. **That looks like hysteresis and isn't.** A grid has boundaries, and a reading
parked on one flips across it on sensor noise alone — logging every single sample,
which is exactly the flood the mechanism exists to prevent.

This is not hypothetical - it is what this code used to do. On the old 0.05V delta,
a rail sitting at 4.900V chattered across the 98/99 bucket boundary once a second
forever. Comparing against the last logged value has no boundaries to sit on.

Guarded by `tests/test_change_detection.py`.

---

## 7. Durability — getting it onto the card

Records buffer in RAM and flush:

- **every 10 seconds**, or
- **immediately** when a sample fails the health check, or
- **immediately** when volts reach the pre-alarm line (4.90V) — while the rail is
  still falling, so the run-up is already saved if it keeps going

Three details that matter more than they look:

- **Flushes are `fsync`ed.** Closing a file only hands data to the kernel's page
  cache, which writes back tens of seconds later on its own schedule. Without the
  `fsync`, the flush interval would buy almost nothing against a hard power cut —
  which is the event this whole thing exists for.
- **A failed write keeps the records buffered** and retries. `buffer_max_records`
  already bounds the buffer, so there is no reason to throw evidence away.
- **`log_write_ok` is published**, so a card refusing writes shows up on the
  dashboard instead of only in a journal nobody reads mid-match.

### What can still be lost

Only records that were buffered but not yet flushed — and during an actual sag the
pre-alarm forces a flush every sample, so that's near zero. What's genuinely at risk
is benign changes while healthy and above 4.90V.

---

## 8. One file per boot

```
health-b00004-20260822-183229.jsonl
       boot   date     time
```

- **Per boot, not per day.** A competition day is many runs and several reboots.
- **Per boot, not per start.** A restart inside the same boot appends to that boot's
  existing file, so a crash loop leaves one readable file instead of a directory full
  of one-record fragments.
- **Boot number leads** so sorting by filename is boot order. Sorting by date would
  trust the clock, and the clock is the one thing here that can't be trusted — a
  single boot with a wrong year would sort to the end and be mistaken for the most
  recent.
- **The date is for humans only.** Nothing depends on it being right.

### Restart loops

`StartLimitIntervalSec=0` means systemd restarts forever and never complains. Correct
for a watchdog, but it means nothing else would flag a service being killed over and
over. So the monitor counts it itself: `starts_this_boot` is published, and past 5 it
logs a warning.

---

## 9. Two watchdogs, and why both

| Failure | Caught by |
|---|---|
| Process crashes and exits | `Restart=always` |
| Process is alive but **stuck** | `Type=notify` + `WatchdogSec=60` |

`Restart=always` only fires when the process exits. A hung process never exits, so it
would sit there looking healthy to systemd while publishing nothing.

The monitor sends `READY=1` once set up, then `WATCHDOG=1` at the end of each
**completed** loop. A hang inside the loop stops the pings and systemd restarts it.

`WatchdogSec=60` against a worst case of roughly 17s — the sample interval plus four
`vcgencmd` timeouts (3s each) plus a `systemctl` check (5s) — leaving room for a slow
card during a flush.

---

## 10. NetworkTables

Published to `/Vision/health`, **numbers and booleans only.** Formatted strings don't
graph in AdvantageScope and can't be consumed without parsing.

- **Everything republishes every sample**, so `heartbeat` increments once a second.
  That is what robot code should watch to know the monitor is alive — not the log,
  which only writes on change.
- **A reading that stops answering publishes `NaN`**, which AdvantageScope draws as a
  gap. Skipping the publish would leave the topic holding its last value, so a dead
  sensor would look like a healthy frozen one.
- **Booleans have no NaN**, so a missing boolean keeps its last state. Nothing better
  is available; worth knowing when reading a trace.
- **`nt_offset_us`** is NT4's client/server clock offset. NT4 syncs the client's clock
  to the server's, which is what lines these records up with roboRIO logs — no OS
  clock sync needed.

---

## 11. Everything is sized against match length

A match is under three minutes. Every interval here is chosen against that, not
against card wear:

| Setting | Value | Reasoning |
|---|---|---|
| `interval_s` | 1.0 | ~150 samples per match |
| `flush_interval_s` | 10.0 | ~7% of a match at risk in RAM |
| `heartbeat_log_s` | 10.0 | a gap in the file localises a death to 10s |
| `service_check_every_n` | 5 | PhotonVision's death is noticed within 5s |

**A heartbeat longer than a match is the trap.** At the original 300s, a clean match
where nothing changed produced no heartbeat at all — "healthy and quiet" looked
identical to "died before the match started."

The card-wear argument for longer intervals doesn't survive contact with
log-on-change: quiet periods write nothing anyway, and a competition day is well under
an hour of actual match time. The card is a consumable; an undiagnosable match isn't.

---

## 12. What it deliberately does not do

- **No vision.** No detection, no pose solving, no calibration — that's PhotonVision.
- **No decisions.** It reports; robot code decides. Anything that acts on a
  measurement belongs in Java where it's testable in simulation.
- **No managing other services.** It reports whether PhotonVision is alive. It does
  not restart it. A watchdog that starts flipping switches is a second failure mode.

---

## 13. Known limits

- **Can't say *why* voltage dropped.** It sees the Pi's 5V side only. Correlating with
  robot battery voltage is done on the roboRIO side — see [RUNNING.md](RUNNING.md).
- **Can miss the shape of a sub-second sag.** Sampling is 1 Hz. The sticky bit still
  catches that it happened, timestamped to within a second.
- **Timestamps are only as good as the clock.** No RTC battery and no time sync means
  the wall clock is wrong until something corrects it. Ordering and intervals still
  hold, and NT4's offset covers the AdvantageScope overlay.
- **Reimaging loses history.** Counters live in `/var/lib/health-monitor`.
- **Nothing is recorded if the service was never enabled.** `install-service.sh`
  checks this, and robot code watching `heartbeat` catches the rest.
- **Old log files are never deleted.** Nothing rotates them.
- **None of the hardware paths have run on a Pi yet.** Everything is verified on a
  laptop — real PMIC parsing, systemd notify, journal persistence and an actual
  unclean boot are all untested.
