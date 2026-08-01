#!/usr/bin/env python3
"""Cross-check the flags a script interprets against the set its guard allows.

The failure this catches: the body handles `--auto`, the guard's allow-set is empty, and a
cron job passing `--auto` dies with exit 2. Comparing call sites is not enough, because an
undocumented caller is invisible - so compare against what the script itself understands.
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {"argv_common.py"}
# Flags that only appear inside the USAGE text are documentation, not interpretation.


def usage_span(t):
    m = re.search(r"USAGE\s*=\s*(?:\"\"\".*?\"\"\"|\".*?\"\n)", t, re.S)
    return (m.start(), m.end()) if m else (-1, -1)


def allowed(t):
    m = re.search(r"cli_guard\(\s*USAGE\s*,\s*(\{.*?\}|set\(\))", t, re.S)
    if m:
        return set(re.findall(r"[\"'](--[a-z0-9-]+)[\"']", m.group(1))) | {"--dry", "--help", "-h"}
    if "add_argument" in t:
        return set(re.findall(r"add_argument\(\s*[\"'](--[a-z0-9-]+)", t)) | {"--dry", "--help", "-h"}
    return None


def interpreted(t, span):
    """Flags the body (excluding the help text) compares against or looks up."""
    s, e = span
    body = t[:s] + t[e:] if s >= 0 else t
    pats = [
        r'=\s*["\'](--[a-z0-9-]+)["\']',
        r'["\'](--[a-z0-9-]+)["\']\s*\)',
        r'["\'](--[a-z0-9-]+)["\']\s+in\s+',
        r'in\s+\(?\s*["\'](--[a-z0-9-]+)["\']',
    ]
    out = set()
    for p in pats:
        out |= set(re.findall(p, body, re.M))
    return out


def main():
    rows = []
    checked = 0
    for f in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
        b = os.path.basename(f)
        if b in SKIP or os.path.islink(f):
            continue
        t = open(f, encoding="utf-8", errors="replace").read()
        al = allowed(t)
        if al is None:
            continue
        checked += 1
        miss = sorted(interpreted(t, usage_span(t)) - al)
        if miss:
            rows.append((b, miss))

    print("=" * 66)
    print(f"flags interpreted by the body but missing from the allow-set ({checked} files)")
    print("=" * 66)
    if rows:
        for b, m in rows:
            print(f"  FAIL {b:32} {' '.join(m)}")
        return 1
    print("  none - everything matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
