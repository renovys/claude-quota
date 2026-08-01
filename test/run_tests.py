#!/usr/bin/env python3
"""Run every check in this directory and exit non-zero if any of them fails.

None of the checks touch the network or your credentials: they compile the sources and
exercise the argument handling only.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def compile_all():
    files = sorted(glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(HERE, "*.py")))
    r = subprocess.run([sys.executable, "-m", "py_compile"] + files,
                       capture_output=True, text=True)
    print("=" * 66)
    print(f"byte-compile: {len(files)} files -> {'ok' if r.returncode == 0 else 'FAIL'}")
    print("=" * 66, flush=True)
    if r.returncode != 0:
        sys.stdout.write(r.stdout + r.stderr)
    return r.returncode


def main():
    rc = compile_all()
    for t in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
        print(flush=True)
        rc |= subprocess.run([sys.executable, t]).returncode
    print()
    print("ALL CHECKS PASSED" if rc == 0 else "SOME CHECKS FAILED")
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
