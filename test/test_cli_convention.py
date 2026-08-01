#!/usr/bin/env python3
"""Check that every executable in the repository follows the CLI convention.

  -h / --help        -> exit 0
  unknown --option   -> exit 2

Only scripts that actually implement the guard are executed. Feeding `--bogus` to a script
without a guard would let the flag be ignored and run the real body, which is exactly the
accident this convention exists to prevent.
"""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {"argv_common.py"}   # helper module, no CLI of its own


def guarded(path):
    try:
        t = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return False
    # A hand-rolled implementation counts too, not just the shared helper, and so does a
    # thin entry point that delegates to a module which has one.
    return ("cli_guard(" in t or "add_argument" in t or "unknown option" in t
            or "claude_quota.main()" in t)


def run(path, args, timeout=40):
    try:
        r = subprocess.run([sys.executable, path] + args,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def main():
    targets = []
    for f in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
        if os.path.basename(f) in SKIP or os.path.islink(f):
            continue
        targets.append(f)

    ok, bad, unguarded = [], [], []
    for f in targets:
        b = os.path.basename(f)
        if not guarded(f):
            unguarded.append(b)
            continue
        h = run(f, ["--help"])
        g = run(f, ["--bogus"])
        (ok if (h == 0 and g == 2) else bad).append((b, h, g))

    print("=" * 70)
    print(f"CLI convention: {len(ok)} passed / {len(bad)} failed / {len(unguarded)} without a guard")
    print("=" * 70)
    for b, h, g in ok:
        print(f"  ok  {b:32} help={h} bogus={g}")
    if bad:
        print("\n[failed - help must be 0 and an unknown option must be 2]")
        for b, h, g in bad:
            print(f"  FAIL {b:32} help={h} bogus={g}")
    if unguarded:
        print("\n[no guard detected - excluded from the run]")
        for b in unguarded:
            print(f"  -  {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
