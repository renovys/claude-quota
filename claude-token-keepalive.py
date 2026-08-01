#!/usr/bin/env python3
"""Refresh Claude access tokens before they expire (meant for an unattended cron job).

`claude_quota.refresh_token` reacts *after* it meets an expired token, and while it works
any other automation that wanted a quota reading is blocked for a moment. Run this script
periodically and only the accounts below the threshold are refreshed ahead of time.

  claude-token-keepalive.py                 check every account, refresh below threshold
  claude-token-keepalive.py --account sub   one account only
  claude-token-keepalive.py --threshold 3   use a 3 hour threshold
  claude-token-keepalive.py --force         refresh regardless of the threshold
  claude-token-keepalive.py --dry           print time left and intent, refresh nothing
"""
try:
    import fcntl  # missing on Windows - without it we proceed unlocked (fail open).
except ImportError:
    fcntl = None
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from argparse import ArgumentParser
from pathlib import Path

# Resolve the sibling modules first, so the tool works from any working directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from argv_common import cli_guard

USAGE = """\
usage: claude-token-keepalive.py [options]

  Look at how long each Claude account's access token is still valid and refresh it
  before it expires when less than the threshold (0.5 hours by default) is left.
  Written for cron, where nobody is watching the output.

options:
  --threshold <hours>      refresh threshold in hours (default 0.5)
  --account <name>|all     which account to check (default all)
  --force                  refresh regardless of the threshold
  --dry                    print time left and intent only, no refresh call
  -h, --help               this help

note: the claude CLI only rotates the access token when it is expired or nearly expired.
      Starting it earlier does not rotate anything, and that is normal - keep the
      threshold low and let an hourly cron catch the moment around expiry.

side effects: for accounts below the threshold it starts the claude CLI briefly (through
        claude_quota.refresh_token) to rotate the OAuth access token. Only an account whose
        credentials cannot be read at all (needs a new login) or whose credentials are
        still expired after the attempt counts as a real failure; no rotation while time is
        still left is the normal waiting state, not a failure. Real failures are reported
        through the command in CLAUDE_QUOTA_NOTIFY_CMD when that variable is set, with a
        6 hour cooldown per account and reason (state file under ~/.claude-quota/). The
        cooldown is recorded only after the command exits 0. Success is never reported.
"""

CACHE_DIR = os.environ.get("CLAUDE_QUOTA_CACHE_DIR") or os.path.expanduser("~/.claude-quota")
LOCK_PATH = os.path.join(CACHE_DIR, "keepalive.lock")
ALERT_STATE = os.path.join(CACHE_DIR, "keepalive-alert.json")
ALERT_COOLDOWN = 6 * 3600
NOTIFY_ENV = "CLAUDE_QUOTA_NOTIFY_CMD"


def _device_label():
    """Machine name to put in the alert, so several machines reporting to the same place
    can be told apart."""
    sysname = platform.system()
    if sysname == "Darwin":
        return "mac"
    if sysname == "Windows":
        return "windows"
    return sysname.lower() or "host"


def _alert_state_load():
    try:
        with open(ALERT_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _alert_recent(key):
    """Cooldown per account and reason."""
    ts = _alert_state_load().get(key)
    try:
        return time.time() - float(ts or 0) < ALERT_COOLDOWN
    except Exception:
        return False


def _alert_mark(key):
    try:
        st = _alert_state_load()
        st[key] = time.time()
        os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
        with open(ALERT_STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def _notify_cmd():
    """Command from CLAUDE_QUOTA_NOTIFY_CMD, split into argv. None when unset."""
    raw = (os.environ.get(NOTIFY_ENV) or "").strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw, posix=(os.name == "posix"))
    except ValueError:
        return None
    return parts or None


def _notify_failure(key, msg):
    """Report a failed refresh through the configured notifier. 6 hour cooldown per
    account and reason; not called under --dry (that path only previews). The cooldown is
    recorded only after the command exits 0, so a failed send is retried next run."""
    if _alert_recent(key):
        return
    cmd = _notify_cmd()
    if not cmd:
        print(f"no notifier configured ({NOTIFY_ENV} is unset): {msg}")
        return
    try:
        r = subprocess.run(cmd + [msg], check=False, timeout=150)
        if r.returncode == 0:
            _alert_mark(key)
            return
        print(f"notification failed: {cmd[0]} exited {r.returncode}")
    except Exception as e:
        print(f"notification failed: {type(e).__name__}: {e}")


def main():
    # The guard runs before locking and permission checks: the --dry verdict and the argv
    # cleanup have to happen before anything else.
    dry_mode = cli_guard(USAGE, {"--threshold", "--account", "--force"})

    # claude_quota strips --dry from sys.argv when it is imported (its own CLI convention).
    # cli_guard already removed it, so this script relies on its own dry_mode instead of
    # that module's DRY flag.
    import claude_quota

    known_accounts = claude_quota.account_names()

    ap = ArgumentParser(add_help=False)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--account", choices=known_accounts + ["all"], default="all")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # --dry has no side effects, so it does not take the global lock.
    if not dry_mode:
        os.makedirs(CACHE_DIR, exist_ok=True)
        lock = open(LOCK_PATH, "w")
        if fcntl is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("another run is in progress, skipping")
                return 0

    accounts = known_accounts if args.account == "all" else [args.account]
    failed = []  # (account, reason_key, message)

    for acc in accounts:
        # Reading while the CLI is writing the credentials can fail to parse, so retry briefly.
        secs = None
        for attempt in range(3):
            _, secs = claude_quota.token_expiry(acc)
            if secs is not None or attempt == 2:
                break
            time.sleep(0.3)
        if secs is None:
            print(f"{acc}: cannot read the credentials -> failure (needs a new login)")
            failed.append((acc, "auth_missing", f"{acc}: cannot read the credentials (needs a new login)"))
            continue

        hours = secs / 3600.0
        need = args.force or hours < args.threshold
        if not need:
            print(f"{acc}: {hours:.1f}h left -> no refresh needed")
            continue

        if dry_mode:
            print(f"{acc}: {hours:.1f}h left -> would refresh (--dry, call skipped)")
            continue

        t0 = time.time()
        ok, why = claude_quota.refresh_token(acc)
        elapsed = time.time() - t0
        if ok:
            print(f"{acc}: {hours:.1f}h left -> refreshed ({elapsed:.1f}s)")
            continue

        # A False from refresh_token is not automatically a problem: the CLI only rotates
        # near expiry, so credentials that are still valid mean we are simply waiting.
        _, secs_after = claude_quota.token_expiry(acc)
        if secs_after is not None and secs_after > 0:
            print(f"{acc}: {hours:.1f}h left -> not time to rotate yet (CLI did not rotate)")
        else:
            print(f"{acc}: {hours:.1f}h left -> refresh failed - {why}")
            failed.append((acc, "refresh_failed", f"{acc}: refresh failed - {why}"))

    if failed:
        for acc, reason, msg in failed:
            key = f"{acc}:{reason}"
            text = f"[{_device_label()}] Claude token keepalive failed - " + msg
            if dry_mode:
                if _alert_recent(key):
                    continue
                route = " ".join(_notify_cmd() or []) or f"none ({NOTIFY_ENV} unset)"
                print(f"{acc}: would notify via {route} (--dry, call skipped) -> {text}")
            else:
                _notify_failure(key, text)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
