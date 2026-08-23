#!/usr/bin/env python3
"""
Pull logs off every device on the robot. Run this from your LAPTOP.

Targets are anything you can SSH into - vision Pis, the roboRIO, a second Pi.
Nothing is installed on them and nothing is deleted; they only have to be
reachable.

    python3 scripts/fetch_logs.py                     # everything in the config
    python3 scripts/fetch_logs.py rio vision-pi       # just these targets
    python3 scripts/fetch_logs.py --list              # what is configured

Standard library only. No rsync (Git Bash on Windows does not ship it) and no
YAML (not installed on every laptop).

Why there is no conversion step for most of this:

  .wpilog   AdvantageScope opens it directly. Nothing to convert.
  .jsonl    Plain text. If the Pi runs as an NT client and the roboRIO runs
            DataLogManager, this data is already inside the .wpilog anyway.
  .hoot     CTRE's format, and closed, so AdvantageScope cannot read it. This
            is the only thing that genuinely needs converting - and `owlet`
            does it from the command line, so you do not need the Tuner GUI.
            If owlet is on PATH, this script runs it for you.
"""

from __future__ import annotations

import argparse
import configparser
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "targets.conf"
DEFAULT_DEST = Path("fetched_logs")

SSH_BASE = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]


def ssh(host: str, command: str, **kwargs):
    return subprocess.run(SSH_BASE + [host, command], **kwargs)


def reachable(host: str) -> bool:
    try:
        return ssh(host, "true", capture_output=True, timeout=20).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def path_kind(host: str, path: str) -> str:
    """'dir', 'file' or 'missing'."""
    r = ssh(host, f"if [ -d '{path}' ]; then echo dir; "
                  f"elif [ -f '{path}' ]; then echo file; else echo missing; fi",
            capture_output=True, text=True)
    return (r.stdout or "missing").strip() or "missing"


def pull_dir(host: str, path: str, dest: Path) -> int:
    """Stream a remote directory over ssh and unpack it. Returns files written."""
    parent, base = str(Path(path).parent).replace("\\", "/"), Path(path).name
    proc = subprocess.Popen(
        SSH_BASE + [host, f"tar czf - -C '{parent}' '{base}' 2>/dev/null"],
        stdout=subprocess.PIPE)
    written = 0
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tf:
            for member in tf:
                if not member.isreg():
                    continue
                try:
                    tf.extract(member, dest, filter="data")
                except TypeError:          # filter= is newer than some 3.11s
                    tf.extract(member, dest)
                written += 1
    except tarfile.TarError:
        pass                                # empty or unreadable; reported by caller
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()
    return written


def pull_file(host: str, path: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        return ssh(host, f"cat '{path}'", stdout=f).returncode == 0


def pull_journal(host: str, unit: str, dest_dir: Path) -> None:
    """This boot and the previous one. After a power cut the previous boot is
    the interesting one, because that is the boot that died."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for label, flag in (("this_boot", "-b"), ("previous_boot", "-b -1")):
        out = dest_dir / f"journal_{unit}_{label}.txt"
        with out.open("w", encoding="utf-8") as f:
            ssh(host, f"journalctl -u {unit} {flag} --no-pager 2>/dev/null || true",
                stdout=f, text=True)
        if out.stat().st_size == 0:
            out.unlink()


def convert_hoot(dest: Path) -> None:
    """CTRE .hoot is closed, so AdvantageScope cannot read it. owlet converts
    it without needing Tuner. Skipped silently when owlet is not installed."""
    hoots = sorted(dest.rglob("*.hoot"))
    if not hoots:
        return
    owlet = shutil.which("owlet")
    if not owlet:
        print(f"  {len(hoots)} .hoot file(s) pulled, but owlet is not on PATH.")
        print("  AdvantageScope cannot read .hoot directly. Get owlet from CTRE's")
        print("  CLI Tools download page, then re-run - no Tuner GUI needed.")
        return
    for h in hoots:
        if h.with_suffix(".wpilog").exists():
            continue
        print(f"  converting {h.name}")
        subprocess.run([owlet, "-f", "wpilog", str(h), str(h.with_suffix(".wpilog"))],
                       check=False)


def fetch(name: str, cfg: configparser.SectionProxy, dest_root: Path) -> dict:
    host = cfg.get("host", "").strip()
    result = {"name": name, "host": host, "files": 0, "skipped": [], "ok": False}
    if not host:
        result["skipped"].append("no host configured")
        return result

    print(f"\n=== {name} ({host}) ===")
    if not reachable(host):
        print("  unreachable - skipping")
        result["skipped"].append("unreachable")
        return result
    result["ok"] = True

    dest = dest_root / name
    dest.mkdir(parents=True, exist_ok=True)

    for path in [p.strip() for p in cfg.get("paths", "").splitlines() if p.strip()]:
        kind = path_kind(host, path)
        if kind == "dir":
            n = pull_dir(host, path, dest)
            print(f"  {path}  ->  {n} file(s)")
            result["files"] += n
        elif kind == "file":
            target = dest / Path(path).name
            if pull_file(host, path, target):
                print(f"  {path}  ->  {target.name}")
                result["files"] += 1
        else:
            print(f"  {path}  ->  not present")

    units = [u.strip() for u in cfg.get("units", "").splitlines() if u.strip()]
    for unit in units:
        pull_journal(host, unit, dest)
    if units:
        print(f"  journals: {', '.join(units)}")

    convert_hoot(dest)
    return result


def summarise(results: list[dict], dest_root: Path) -> None:
    print("\n=== summary ===")
    for r in results:
        state = "ok" if r["ok"] else "SKIPPED (%s)" % ", ".join(r["skipped"])
        print(f"  {r['name']:<14} {r['files']:>4} file(s)  {state}")

    faults = 0
    for f in dest_root.rglob("*.jsonl"):
        with f.open(encoding="utf-8", errors="replace") as fh:
            faults += sum(1 for line in fh if '"ok": false' in line)
    if faults:
        print(f"\n  {faults} sample(s) with ok=false")

    brownouts = [p.name for p in dest_root.rglob("journal_*.txt")
                 if "BROWNOUT" in p.read_text(encoding="utf-8", errors="replace")]
    for b in brownouts:
        print(f"  BROWNOUT reported in {b}")

    print(f"\nEverything is under {dest_root}. Nothing was deleted on any device.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pull logs off the robot's devices")
    ap.add_argument("targets", nargs="*", help="names from the config; default is all")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--dest", default=str(DEFAULT_DEST))
    ap.add_argument("--list", action="store_true", help="show configured targets and exit")
    args = ap.parse_args(argv)

    cfg = configparser.ConfigParser()
    if not cfg.read(args.config, encoding="utf-8"):
        print(f"No config at {args.config}", file=sys.stderr)
        return 1

    names = [s for s in cfg.sections() if s != "DEFAULT"]
    if args.list:
        for n in names:
            print(f"{n:<14} {cfg[n].get('host', '(no host)')}")
        return 0

    wanted = args.targets or names
    unknown = [t for t in wanted if t not in names]
    if unknown:
        print(f"Unknown target(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"Configured: {', '.join(names)}", file=sys.stderr)
        return 1

    dest_root = Path(args.dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    results = [fetch(n, cfg[n], dest_root) for n in wanted]
    summarise(results, dest_root)
    return 0 if any(r["ok"] for r in results) else 1

if __name__ == "__main__":
    sys.exit(main())
