# Troubleshooting Log

*A running log of problems we hit and how we solved them. Append as you go.*

**Scope:** the whole Pi platform, not just the health monitor — imaging, boot, power,
SSH, services. It lives in this repo because this is the only repo that exists yet.
Promote it to a shared docs repo when there is one.

---

## How to use this

**Add an entry when you lose more than fifteen minutes to something.** If it cost you
time, it will cost the next person time too.

Write it while it's fresh. An entry written the next morning is missing the details
that actually mattered, and an entry you meant to write is worth nothing.

**Newest entries go at the top**, directly under the Entries heading.

Don't polish. A rough entry that exists beats a good one that doesn't. Nobody is
grading the prose. If you only have two minutes, fill in **Symptom** and **Fix** and
move on.

---

## The heuristic that keeps proving right

**The error message names the last thing that failed, not the first thing that's wrong.**

We learned this the hard way during initial imaging. "Media error detected" was
antivirus. "Waiting for network" was a blank SD card. "Connection refused" was a
service that had never been enabled. In every case the message pointed at a symptom
several steps downstream of the cause.

Two questions that repeatedly cut through it:

**Does the failure point move?** A process that fails at the same place every time is
hitting a defect at a fixed location. One that fails somewhere different each time is
hitting a connection, a timeout, or something racing. This single question separates
bad hardware from flaky connections faster than anything else.

**What changed?** Not "what's broken" — what *changed* since it last worked. Usually
the answer is right there and boring.

And before deep-diving software: **physically reseat the cable.** It's cheap to check
and embarrassing to discover late. It has already cost us an evening once.

---

## First checks, before you believe anything else

### Power

```bash
vcgencmd get_throttled
```

`0x0` is the only clean answer. Anything else, read the bits — and remember **bits
16–19 are sticky history that survives a soft reboot**, so they may be describing a
problem you already fixed. Bits 0–3 are live state. See the README for the full table.

The health monitor separates these for you and additionally reports
`undervoltage_since_start` / `throttled_since_start` — what has gone wrong *since the
service started watching*, which is the number that means something during a match.

```bash
python3 src/health_monitor.py --once     # safe on a running Pi
journalctl -u health-monitor -b          # what it saw since boot
```

### Which OpenCV is actually loaded?

If OpenCV behaves in a way the documentation says it shouldn't, check which build
Python is importing before debugging anything else:

```bash
python3 -c "import cv2; print(cv2.__version__); print(cv2.__file__)"
```

`dist-packages` means apt (what we want — 4.10.x, contrib included). `site-packages`
means pip. If both are installed, Python silently takes whichever appears first on
`sys.path`, and you can spend a long time reading docs for a version you aren't
running. `cv2.aruco` missing is the usual symptom.

---

## Entry template

Copy this block for a new entry.

```markdown
## YYYY-MM-DD — Short description

**Symptom:** What was actually observed. Exact error text if there was one.

**What we thought it was:** The wrong guesses, in order. This field matters — it
stops the next person walking the same dead ends.

**Root cause:** What it actually was.

**Fix:** What resolved it. Commands, settings, part numbers.

**Recognising it next time:** The one check that would have identified this in
two minutes instead of two hours.
```

---
---

# Entries

## 2026-08-22 — SSH "connection refused" after hostname change

**Symptom:** `ssh user@newname.local` returned `Connection refused` immediately. Had been working before the rename.

**What we thought it was:** Broken mDNS after the rename. Then a firewall. Then a wrong username.

**Root cause:** "Connection refused" means the host was reached and something actively declined — a genuinely dead or unreachable host gives a *timeout* instead. So the name was resolving fine. The SSH daemon had been started manually but never enabled, so it did not come back after the reboot that applied the hostname change.

**Fix:**
```bash
sudo systemctl enable --now ssh
```

**Recognising it next time:** Refused vs timeout is the tell. Refused = reached it, nothing listening on that port. Timeout = didn't reach it at all. Different problems entirely.

> **Generalise this one.** `systemctl start` runs a service now. `systemctl enable` makes it run at boot. They are independent. A service that was started but never enabled works perfectly until the first power cycle and then silently doesn't — which on a robot means it dies at the venue, not on the bench. Always `enable --now`.
>
> This is exactly why `scripts/install-service.sh` uses `enable --now` and says so in a comment.

---

## 2026-08-22 — Windows SSH: "Bad owner or permissions on config"

**Symptom:** Every `ssh` command on Windows failed before connecting:
```
Bad permissions. Try removing permissions for user: UNKNOWN\UNKNOWN (S-1-5-21-...)
Bad owner or permissions on C:\Users\<user>\.ssh\config
```

**What we thought it was:** Something wrong on the Pi. It wasn't — this never left the laptop.

**Root cause:** The `.ssh\config` file carried an ACL entry for a Windows SID that no longer resolves to an account. OpenSSH refuses to read a config file whose permissions it can't verify.

**Fix:** The file is optional (it only stores host shortcuts), so renaming it was enough:
```
ren C:\Users\<user>\.ssh\config config.bak
```
To keep it instead, take ownership, strip inheritance, grant only your own account, and remove the orphaned SID with `takeown` and `icacls`.

**Recognising it next time:** If the error names a local file path, the problem is local. Nothing had reached the network yet.

---

## 2026-08-21 — `get_throttled` returns `0x50000`

**Symptom:** After `sudo apt full-upgrade` on a fresh image, `vcgencmd get_throttled` returned `0x50000` instead of `0x0`.

**What we thought it was:** Briefly suspected the SD card, since we'd already had card trouble.

**Root cause:** Underpowered supply. `0x50000` sets bits 16 and 18 — under-voltage *has occurred* and throttling *has occurred*, both historical since boot rather than currently active (bits 0–3 were clear). The bootloader screen had shown `power: supply: USB-C 900 mA` the whole time. The Pi 5 wants **5V/5A (27W)**. During the upgrade, CPU and SD card were hammered simultaneously, current draw exceeded what the supply could deliver, voltage sagged, and the Pi clocked down to survive.

**Fix:** Proper 27W USB-C PD supply. On the robot this becomes a regulated 12V→5V converter sized for 5A, fed from a dedicated protected circuit — **never** the roboRIO's USB ports or the VRM. Candidates being sourced: Pololu D36V50F5, Redux Zinc-V, Grapple MitoCANdria.

**Recognising it next time:** `vcgencmd get_throttled` is the first command to run when anything is weird. `0x0` is the only fully clean result. Bits 0–3 are live state; bits 16–19 are sticky history that only clears on a **full power cycle**, not a soft reboot.

> Underpowering also causes intermittent freezes, USB devices dropping out, cameras disconnecting mid-stream, and SD card corruption. On a robot it will look exactly like a code bug. Log this value during practice matches.

> **This entry is why the health monitor exists**, and it is still the live state of the
> dev Pi as of 2026-08-22. Two things to do once the proper supply arrives:
> **pull the plug** to clear the sticky bits (a soft reboot won't, and you'll think the
> fix failed), then confirm `0x0`. After that, `throttled_since_start` in the monitor
> becomes a trustworthy live signal rather than a restatement of this history.

---

## 2026-08-21 — Pi hangs at "waiting for network"

**Symptom:** Pi 5 booted to a diagnostic screen and sat indefinitely at `waiting for network`. Ethernet was connected and confirmed working from a laptop on the same cable.

**What we thought it was:** A network or DHCP problem. Spent time on the router.

**Root cause:** Not a network problem at all. Having found nothing bootable on SD or USB, the bootloader fell through to TFTP network boot, looking for a PXE server that doesn't exist on our network. The message means "no valid boot media," not "bad ethernet."

The giveaway was on screen the whole time: `part: 0 mbr [0x00:00000000 0x00:00000000 ...]` — an entirely empty partition table. A valid Pi OS card shows **two** partitions, a small FAT32 boot partition and a larger ext4 root.

**Fix:** Write a valid image. We ended up using Network Install (hold **SHIFT** at power-on, wired ethernet required) which writes the card through the Pi's own SD slot and bypasses the laptop card reader entirely.

**Recognising it next time:** Read the `part:` line before anything else. All zeros means nothing was written, full stop. Also worth doing the cluster math on the `type: 32` line to sanity-check capacity — `clusters × sectors × 512 bytes` should match the card's advertised size.

---

## 2026-08-21 — SD write verification failing on multiple cards

**Symptom:** Raspberry Pi Imager completed the write, then failed at verification: `Verifying write failed. Contents of SD card is different from what was written to it.` Happened on two different cards from unrelated sources.

**What we thought it was:** Counterfeit card. That's the classic signature — fake cards report a large capacity with far less real flash, and writes past the real capacity silently vanish, which surfaces at exactly this point.

**Root cause:** The USB card reader. Two cards from *unrelated* sources (one borrowed, one an old spare) failing identically is strong evidence against counterfeits, which come in batches. The write may have been fine; the readback was corrupted.

**Fix:** Bypassed the reader entirely using Pi 5 Network Install.

**Recognising it next time:** **Same failure on two or more unrelated cards means it's the reader.** One card only means suspect the card — confirm with h2testw, which fills the card and verifies every byte, reporting actual usable capacity.

> Also worth knowing: a verification failure doesn't guarantee unbootable media. Just trying to boot it is a two-minute test.

---

## 2026-08-21 — Imager: "Access denied error while writing file to disk"

**Symptom:** Imager failed immediately on clicking Write. The drive then vanished from File Explorer until a letter was reassigned in Disk Management.

**What we thought it was:** The disappearing drive looked like the problem. It isn't — Imager **dismounts the volume** so it can write raw sectors. That's expected behaviour.

**Root cause:** Norton Utilities Premium ships **Data Protector**, which specifically intercepts raw block-level writes to disks — precisely what Imager does. Compounded by Windows holding a lock on the freshly formatted volume.

**Fix:** Disabled Norton Auto-Protect and Data Protector, ran Imager as administrator, then cleared the card with `diskpart clean`.

**Recognising it next time:** Check in this order — physical lock switch on the SD adapter, run as admin, antivirus, close anything touching the drive, then `diskpart clean`.

> ⚠️ `diskpart clean` is destructive with no confirmation prompt. **Verify the disk number by capacity every single time.**

> Also: **don't pre-format the card.** Imager overwrites the whole device including the partition table. Formatting first only gives Windows another chance to lock it.

---

## 2026-08-21 — Imager: "Media error detected" partway through

**Symptom:** Write progressed to roughly 22%, then failed with `Media error detected. The storage device may be damaged or counterfeit.` A later attempt failed past 80%.

**What we thought it was:** Bad card, initially.

**Root cause:** The failure point **moved** between attempts — 22%, then 80%+. A physical bad block sits at a fixed offset and would fail at the same percentage every time. A moving target points at a connection or device dropout. In our case, a timed antivirus exemption expiring mid-write, compounded by USB power management.

**Fix:** Disabled antivirus for longer than the write would take, switched from a vendor power plan ("Norton Utilities Premium — Ultra Performance") to stock **High performance**, disabled USB selective suspend and sleep, and stayed at the machine.

**Recognising it next time:** **Does the failure point move?** Same percentage every time means the card. Different percentage each time means reader, port, power management, or antivirus re-arming.

> Vendor power plans can strip the advanced power settings dialog and may silently reassert themselves. Re-check the active plan right before writing.

---

*These entries are the condensed, searchable versions of the imaging saga. The
longer-form write-up is not in this repo — if you still have it, park it somewhere the
whole team can reach and link it here.*
