#!/usr/bin/env python3
"""Show every configured Claude account on one screen as a bar chart (read only)."""

import sys
from pathlib import Path

# Resolve the sibling modules first, so the tool works from any working directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import json
import os
import subprocess
import unicodedata
from datetime import datetime

from argv_common import cli_guard

# Keep bars readable on a Windows console whose default code page is not UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

USAGE = """\
usage: quota-all.py [options]

  Show the 5-hour and weekly usage of every configured Claude account as a bar chart.
  Accounts come from CLAUDE_QUOTA_ACCOUNTS (see claude_quota.py); by default that is
  ~/.claude plus ~/.claude-sub when it exists.

options:
  --refresh    ignore the 60 second cache and query the limits again
  --no-refresh never start the claude CLI to refresh an expired token (query only)
  --json       print the raw data as JSON instead of the table
  --dry        only disable the automatic token refresh; the query itself still runs
  -h, --help   this help

side effects: reading only. It calls the Claude OAuth usage API, which does not consume
        any of the usage it reports. One exception: if an account's access token has
        expired, claude-quota.py starts the claude CLI for a few seconds with that
        config directory and kills it as soon as the token is rotated (pass --no-refresh
        to disable). The only files written are the quota cache and the refresh cooldown
        marker under ~/.claude-quota/.
"""

LOCAL_TZ = datetime.now().astimezone().tzinfo


def _find_claude_quota():
    """Locate the query backend next to this script.

    Both names are accepted: the hyphenated `claude-quota.py` is the CLI entry point and
    `claude_quota.py` is the importable module. Both accept `--json`.
    """
    for name in ("claude-quota.py", "claude_quota.py"):
        cand = HERE / name
        if cand.is_file():
            return str(cand)
    return str(HERE / "claude-quota.py")


# Run the backend through the interpreter explicitly - on Windows there is no exec bit
# and no shebang handling.
CLAUDE_QUOTA = _find_claude_quota()


def claude_data(refresh, no_refresh=False):
    cmd = [sys.executable, CLAUDE_QUOTA, "--json"]
    if refresh:
        cmd.append("--refresh")
    if no_refresh:  # do not start the CLI even on an expired token (implied by --dry)
        cmd.append("--no-refresh")
    try:
        # The encoding is pinned on purpose. On Windows text=True decodes with the ANSI
        # code page and can raise UnicodeDecodeError on non-ASCII JSON, leaving stdout as
        # None. PYTHONIOENCODING forces the child to **write** UTF-8 for the same reason.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        out = subprocess.run(
            cmd, capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"_error": "claude-quota.py did not answer within 180s"}
    if out.returncode != 0:
        msg = (out.stderr or out.stdout or "").strip().splitlines()
        return {"_error": f"claude-quota.py failed (rc={out.returncode}): {msg[-1] if msg else 'no output'}"}
    try:
        return json.loads(out.stdout or "")
    except (json.JSONDecodeError, TypeError):
        return {"_error": "claude-quota.py did not print JSON"}


def gpt_data():
    """Optional extra provider. Nothing ships with this repository, so the section is
    simply left out unless a `gpt_quota` module with an `info()` function is importable."""
    try:
        import gpt_quota
    except Exception:
        return None
    try:
        return dict(gpt_quota.info() or {})
    except Exception as exc:  # a failure here must not hide the Claude numbers
        return {"_error": f"{type(exc).__name__}: {exc}"}


def remain(iso):
    """Time left until an ISO timestamp, rendered as "4h 19m"."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(str(iso))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=LOCAL_TZ)
    sec = (when - datetime.now(LOCAL_TZ)).total_seconds()
    if sec <= 0:
        return "reset"
    d, rest = divmod(int(sec), 86400)
    h, m = divmod(rest // 60, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def when_str(iso):
    if not iso:
        return "?"
    try:
        when = datetime.fromisoformat(str(iso))
    except ValueError:
        return "?"
    if when.tzinfo is None:
        when = when.replace(tzinfo=LOCAL_TZ)
    when = when.astimezone(LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).date()
    if when.date() == today:
        return f"today {when:%H:%M}"
    if (when.date() - today).days == 1:
        return f"tomorrow {when:%H:%M}"
    return f"{when:%m/%d %H:%M}"


def bar(pct, width=20):
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "·" * (width - filled)


def mark(pct):
    pct = float(pct or 0)
    if pct >= 90:
        return "🔴"
    if pct >= 70:
        return "🟡"
    return "🟢"


def pad(text, width):
    """Pad on the right, counting wide characters as two columns."""
    used = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - used)


def line(name, pct, reset_iso, tail=""):
    return (
        f"  {pad(name, 16)} {mark(pct)} {bar(pct)} {float(pct or 0):5.1f}%"
        f"   resets {when_str(reset_iso)} ({remain(reset_iso)}){tail}"
    )


def render(claude, gpt):
    rows = []
    now = datetime.now(LOCAL_TZ)
    rows.append(f"Usage limits  (as of {now:%m/%d %H:%M})")

    if claude.get("_error"):
        rows.append(f"  ! Claude query failed - {claude['_error']}")
    else:
        for key in claude:
            acc = claude.get(key)
            title = f"Claude [{key}]"
            rows.append("")
            if not acc:
                rows.append(f"{title} - no result")
                continue
            # claude-quota.py never raises; it fills the `error` key instead. Ignoring that
            # would draw a failure as "0.0%" and make the account look completely unused.
            if acc.get("error"):
                kind = acc.get("error_kind")
                hint = "  -> the automatic refresh failed too; log in to that account again" \
                    if kind == "auth_expired" else ""
                rows.append(f"{title}  ! query failed - {acc['error']}{hint}")
                continue
            plan = str(acc.get("plan") or "?").upper()
            rows.append(f"{title}  [{plan}]  {acc.get('email', '')}")
            rows.append(line("5-hour", acc.get("session_pct"), acc.get("session_reset")))
            rows.append(line("weekly", acc.get("weekly_pct"), acc.get("weekly_reset")))
            for model, mpct in (acc.get("scoped") or {}).items():
                reset = (acc.get("scoped_reset") or {}).get(model)
                rows.append(line(f"weekly/{model}", mpct, reset))

    if gpt is not None:
        rows.append("")
        if gpt.get("_error"):
            rows.append(f"Extra provider\n  ! query failed - {gpt['_error']}")
        else:
            plan = str(gpt.get("plan") or "?").upper()
            age = gpt.get("age_hours")
            tail = ""
            if isinstance(age, (int, float)):
                tail = f"   measured {age:.1f}h ago"
                if age >= 12:
                    tail += " (stale)"
            rows.append(f"Extra provider  [{plan}]  source {gpt.get('host', '?')}")
            rows.append(line("weekly", gpt.get("pct"), gpt.get("resets_at"), tail))

    return "\n".join(rows)


def main():
    dry = cli_guard(USAGE, {"--refresh", "--json", "--no-refresh"}, dry_readonly=True)
    argv = set(sys.argv[1:])
    claude = claude_data("--refresh" in argv, no_refresh=("--no-refresh" in argv or dry))
    gpt = gpt_data()

    if "--json" in argv:
        payload = {"claude": claude}
        if gpt is not None:
            payload["gpt"] = gpt
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(claude, gpt))

    return 1 if claude.get("_error") else 0


if __name__ == "__main__":
    sys.exit(main())
