# Troubleshooting Log

A running log of problems this team has actually hit, and what turned out to be wrong.
Not a polished guide. Messy is fine. Half-remembered is fine. **Nothing is better than
a rough entry.**

**Newest entries at the top.** Add yours as soon as you solve something, not "later" —
later does not happen, and the detail you skip is always the one the next person needs.

---

## Before you start debugging: read this

### The error message names the last thing that failed, not the first thing that is wrong

This is the most useful thing this team has learned, and it has cost us real weekends.

- A "media error" during SD imaging turned out to be **antivirus** locking the card.
- A "waiting for network" hang on boot turned out to be a **blank SD card**.

Neither message pointed anywhere near the actual cause. **Work outward from software
to hardware**, and do not trust the wording of the failure.

### Does the failure point move?

When something fails repeatedly, notice *where*:

- **Fixed failure point** — same place, same way, every time. That is a **defect**:
  bad file, bad config, bad card, genuine bug.
- **Moving failure point** — different place each attempt. That is a **connection or
  timing problem**: power, a loose cable, a marginal USB port, a race on startup.

This one question routinely saves an hour.

### Check power before you believe anything else

On a Pi, the majority of "weird" is power. Run this first, always:

```bash
vcgencmd get_throttled
```

`0x0` is the only clean answer. Anything else means read the bits — and remember bits
16-19 are sticky history that survives a soft reboot, so they may be describing a
problem you already fixed. See the README for the full bit table.

### Which OpenCV is actually loaded?

If OpenCV behaves in a way the documentation says it should not, check which build
Python is importing before debugging anything else:

```bash
python3 -c "import cv2; print(cv2.__version__); print(cv2.__file__)"
```

`dist-packages` means apt (what we want, 4.10.x, contrib included). `site-packages`
means pip. If both are installed, Python silently takes whichever appears first on
`sys.path`, and you can spend a long time reading docs for a version you are not
running. `cv2.aruco` missing is the usual symptom.

---

## How to add an entry

Copy the template, fill it in, put it directly under this line. Do not delete or tidy
old entries — a solved problem coming back is itself useful information.

If you only have two minutes, fill in **Symptom** and **Fix** and move on. A partial
entry beats an intended one.

```markdown
## YYYY-MM-DD — Short description of the symptom

**Who:** name(s)

**Symptom**
What you actually saw. Exact error text if you have it, in the words of whoever
saw it. Not your theory about it — that goes in the next field.

**What we thought it was**
The wrong guesses, in order, and how each was ruled out. This field is not
optional and it is not embarrassing. It is the field that stops the next person
walking the same dead end.

**Root cause**
What was actually wrong.

**Fix**
What made it work. Commands, settings, the part that got replaced.

**How to recognise it next time**
The one specific check that would identify this quickly. Ideally a command and
the output that confirms it.
```

---

## 2026-08-22 — Vision freezes and USB cameras drop out at random

**Who:** vision subteam

**Symptom**

The Pi 5 would run normally for a while, then vision would freeze. Sometimes a USB
camera vanished from `/dev/video*` entirely and only came back after a replug.
Occasionally the whole Pi rebooted on its own with nothing in the logs explaining why.
No pattern anyone could pin down — it seemed to happen more under load, but not
reliably enough to reproduce on demand.

**What we thought it was**

1. *A bug in the vision code.* First instinct, and wrong. Ruled out when the freezes
   kept happening with our own scripts stopped entirely.
2. *A bad USB camera or cable.* Swapped both. No change.
3. *An overheating SoC.* Plausible for a Pi 5 in an enclosure. `vcgencmd measure_temp`
   read well under the throttle point during a freeze, so no.
4. *A corrupt SD card.* We were about to reimage — which would have destroyed the
   evidence and taught us nothing — when someone finally ran `get_throttled`.

**Root cause**

**Under-voltage.** The Pi was being run from a spare **900mA USB-C phone charger.**
A Pi 5 wants **5V/5A (27W)**; 900mA is not close. Under load the supply sagged, the
firmware capped the clock and throttled, and USB ports — which share that budget —
browned out and dropped their devices.

Every symptom above is a documented consequence of insufficient supply. None of them
say "power" anywhere in the error text.

**Fix**

Immediate: run the Pi from a proper 5V/5A USB-C supply and confirm `get_throttled`
returns `0x0` after a **full power cycle** (a soft reboot will not clear the sticky
bits and you will think the fix failed).

On the robot, a phone charger is not an option — the Pi runs off the 12V battery
through a regulator. Currently being sourced, all rated well above the Pi's draw:

- Pololu D36V50F5
- Redux Zinc-V
- Grapple MitoCANdria

Undersizing this regulator recreates the exact same problem on the robot, except now
it only appears during hard acceleration, at a competition, in front of people.

**How to recognise it next time**

```bash
vcgencmd get_throttled
```

`throttled=0x50000` — bits 16 and 18 — means **under-voltage has occurred** and
**throttling has occurred** since boot. That is this problem.

Two things that will confuse you:

- **The sticky bits do not clear on a soft reboot.** Only a full power cycle clears
  them. After fixing the supply, pull the plug — otherwise you will see `0x50000` on
  a healthy Pi and conclude the fix did not work.
- **Bits 0-3 versus 16-19 mean different things.** `0x50000` says it happened at some
  point since boot; it does not say it is happening now. If bit 0 or bit 2 is set, it
  is happening *right now*. The health monitor separates these, and additionally
  reports what has gone wrong *since the service started* — which is the number that
  actually means something during a match.

> This entry is why the health monitor exists. Four wrong hypotheses, a camera swap,
> and a nearly-wiped SD card, when one command answered it in a second.
