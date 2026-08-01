#!/usr/bin/env python3
"""Single source for reading Claude account usage limits.

Reads the 5-hour window, the weekly window and their reset times straight from the
official OAuth usage endpoint that the Claude Code CLI itself uses. Import it as a
library or run it as a CLI:

  claude-quota.py                  # human readable summary of every account
  claude-quota.py --json           # machine readable JSON
  claude-quota.py --account sub    # a single account
  claude-quota.py --refresh        # ignore the cache and query again
  claude-quota.py --no-refresh     # never launch the CLI, even on an expired token

If the access token is expired (or about to expire) or the endpoint answers 401/403, the
CLI is started briefly with that account's config directory and killed as soon as the
credentials are rotated, then the query is retried once. That way an account you have not
used for a while still answers without a manual login.

As a library:  from claude_quota import quota  ->  quota("main")
Accounts are separated by CLAUDE_CONFIG_DIR. See ACCOUNTS below.
"""
import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    import fcntl  # missing on Windows - without it we proceed unlocked (fail open).
except ImportError:
    fcntl = None

# CLI convention: --dry has to be blocked at the top of every function with side effects.
# It must be known before the parser runs, so sys.argv is inspected directly.
DRY = "--dry" in sys.argv[1:]
# Once decided, drop it from argv. Leaving it in makes argparse report the flag after a
# subcommand as unrecognized and exit 2.
sys.argv[1:] = [_a for _a in sys.argv[1:] if _a != "--dry"]

LOCAL_TZ = datetime.now().astimezone().tzinfo
OAUTH_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_DIR = os.environ.get("CLAUDE_QUOTA_CACHE_DIR") or os.path.expanduser("~/.claude-quota")
CACHE_TTL = 60  # seconds. Saves the rate limit bucket when the same account is polled twice.

HOME = os.path.expanduser("~")
DEFAULT_CONFIG_DIR = os.path.join(HOME, ".claude")


def _account_entry(name, config_dir):
    config_dir = os.path.abspath(os.path.expanduser(config_dir))
    # The default config directory keeps its account metadata in ~/.claude.json, every
    # other CLAUDE_CONFIG_DIR keeps it inside the directory itself.
    meta = (os.path.join(HOME, ".claude.json") if config_dir == DEFAULT_CONFIG_DIR
            else os.path.join(config_dir, ".claude.json"))
    return {
        "label": name,
        "config_dir": config_dir,
        "creds": os.path.join(config_dir, ".credentials.json"),
        "meta": meta,
    }


def _load_accounts():
    """Account table.

    Override it with CLAUDE_QUOTA_ACCOUNTS, a comma separated list of name=config_dir:

        CLAUDE_QUOTA_ACCOUNTS="work=~/.claude,personal=~/.claude-alt"

    Without the variable, "main" is the default config directory and "sub" is
    ~/.claude-sub when that directory exists.
    """
    spec = os.environ.get("CLAUDE_QUOTA_ACCOUNTS")
    if spec:
        out = {}
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            name, _, path = part.partition("=")
            name = name.strip()
            path = path.strip()
            if name and path:
                out[name] = _account_entry(name, path)
        if out:
            return out
    out = {"main": _account_entry("main", DEFAULT_CONFIG_DIR)}
    alt = os.path.join(HOME, ".claude-sub")
    if os.path.isdir(alt):
        out["sub"] = _account_entry("sub", alt)
    return out


ACCOUNTS = _load_accounts()


def account_names():
    """Configured account names, in order."""
    return list(ACCOUNTS)


def _cli_version():
    """CLI version for the User-Agent. Without it the request lands in a harsher rate
    limit bucket, so it is always sent."""
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10).stdout
        return out.strip().split()[0]
    except Exception:
        return "2.1.220"


def _keychain_token(creds_path):
    """macOS keeps the credentials in the login keychain instead of a file.

    Linux and Windows use `<config dir>/.credentials.json`, but on macOS that file does not
    exist and the same JSON lives in a `security` item. If nothing is found we return None
    and the caller treats it as auth_missing (no file and no keychain item means the
    account was never logged in).
    """
    if sys.platform != "darwin":
        return None
    config_dir = os.path.dirname(creds_path)
    # Naming rule: the default config directory (~/.claude) uses "Claude Code-credentials"
    # with no suffix; any other CLAUDE_CONFIG_DIR appends the **first 8 hex characters of
    # the sha256 of its absolute path**, e.g. "Claude Code-credentials-eef327c1".
    svce = "Claude Code-credentials"
    if os.path.abspath(config_dir) != DEFAULT_CONFIG_DIR:
        svce += "-" + hashlib.sha256(config_dir.encode()).hexdigest()[:8]
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", svce, "-w"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout).get("claudeAiOauth", {})
    except Exception:
        pass
    return None


def _read_token(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("claudeAiOauth", {})
    except FileNotFoundError:
        tok = _keychain_token(path)
        if tok is None:
            raise
        return tok


def _read_email(path):
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("oauthAccount") or {}).get("emailAddress")
    except Exception:
        return None


def _to_local(iso):
    """Convert the UTC ISO string from the endpoint to local time. None stays None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ).isoformat(timespec="seconds")
    except Exception:
        return iso


def _cache_path(account):
    return os.path.join(CACHE_DIR, f"quota-cache-{account}.json")


def _load_cache(account, ttl):
    p = _cache_path(account)
    try:
        if time.time() - os.path.getmtime(p) > ttl:
            return None
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["cached"] = True
        return d
    except Exception:
        return None


def _save_cache(account, data):
    if DRY:
        print("[dry] skipped writing the quota cache")
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = _cache_path(account)
    tmp = f"{p}.tmp.{os.getpid()}"   # pid suffix so parallel calls do not share a temp file
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


class TokenMissing(Exception):
    """The credentials hold no access token - that account was never logged in."""


class TokenExpired(Exception):
    """The access token is expired or about to expire. Only the Claude Code CLI can renew
    it, so the caller has to run the CLI once for that account and query again."""


def token_expiry(account):
    """Return (expiry datetime, seconds left). (None, None) when it cannot be read."""
    try:
        v = _read_token(ACCOUNTS[account]["creds"]).get("expiresAt")
        dt = datetime.fromtimestamp(v / 1000, LOCAL_TZ)
        return dt, (dt - datetime.now(LOCAL_TZ)).total_seconds()
    except Exception:
        return None, None


# -- automatic token refresh -------------------------------------------------------------
# The access token (roughly 8 hours) is renewed by the Claude Code CLI itself. This module
# only reads files, so an account whose CLI has not run for a while ends up with an expired
# token and a failing usage query. A secondary account is the usual victim: no CLI run means
# no refresh, no refresh means the query fails. To break that loop we start the CLI
# **briefly** with that config directory and kill it again.
#
# Measured behaviour: the refresh happens right before the **first API request**, not at
# startup.
#   - `claude auth status`, or an interactive session idling for 40 seconds: no refresh
#   - `claude -p ...`: refresh happens, even when the 5-hour window is at 100% and the
#     call itself fails
# So we launch it with `-p` and kill the process as soon as new credentials are written.
# The refresh completes before the model call, so it usually costs no inference tokens.
#
# Careful: this is a **refresh token rotation**. The old token dies immediately, so running
# it repeatedly in a short window is dangerous (rotating five times in fifteen minutes once
# produced a 401 and the CLI erased the credentials, logging the account out). Hence
# (1) a 30 minute cooldown, (2) a two second settle before killing the process so it is
# never interrupted mid-write, and (3) a check that the accessToken itself changed, not
# only expiresAt.
REFRESH_GUARD_ENV = "CLAUDE_QUOTA_NO_REFRESH"  # stops a child session from refreshing again
REFRESH_COOLDOWN = 1800  # seconds. A refresh is a token **rotation**, so do not repeat it.
REFRESH_SETTLE = 2.0     # seconds to wait after the credentials changed before killing.


def _expiry_ms(account):
    """expiresAt (ms) from the credentials, or None. _read_token covers the macOS keychain."""
    try:
        return _read_token(ACCOUNTS[account]["creds"]).get("expiresAt")
    except Exception:
        return None


def _token_fingerprint(account):
    """(expiresAt, first 16 chars of sha256 over the whole accessToken). Used to confirm a
    refresh from the token itself. OAuth tokens share a long prefix, so comparing only the
    first N characters can make two different tokens look identical - hash all of it. The
    raw token is never stored."""
    try:
        t = _read_token(ACCOUNTS[account]["creds"])
        tok = t.get("accessToken") or ""
        digest = hashlib.sha256(tok.encode()).hexdigest()[:16] if tok else ""
        return t.get("expiresAt"), digest
    except Exception:
        return None, None


def _claude_bin():
    """The claude executable. cron and systemd have a narrow PATH, so known install
    locations are checked too."""
    p = shutil.which("claude")
    if p:
        return p
    for c in (os.path.join(HOME, ".local", "bin", "claude"),
              os.path.join(HOME, ".claude", "local", "claude"),
              os.path.join(HOME, "AppData", "Local", "Programs", "claude", "claude.exe")):
        if os.path.exists(c):
            return c
    return None


def _refresh_state_path(account):
    return os.path.join(CACHE_DIR, f"refresh-{account}.json")


def _refresh_lock_path(account):
    return os.path.join(CACHE_DIR, f"refresh-{account}.lock")


def _refresh_recent(account):
    try:
        with open(_refresh_state_path(account), encoding="utf-8") as f:
            return time.time() - float(json.load(f).get("ts") or 0) < REFRESH_COOLDOWN
    except Exception:
        return False


def _refresh_mark(account):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_refresh_state_path(account), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time()}, f)
    except Exception:
        pass


def _acquire_refresh_lock(account):
    """Non-blocking flock on a per-account lock file. Returns (lock_file, ok, reason).
    Without fcntl (Windows) it passes through unlocked (fail open)."""
    if fcntl is None:
        return None, True, None
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        f = open(_refresh_lock_path(account), "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f, True, None
    except BlockingIOError:
        return None, False, "another process is already refreshing"
    except Exception:
        return None, True, None  # a broken locking mechanism fails open


def _release_refresh_lock(f):
    if f is None:
        return
    try:
        fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        f.close()
    except Exception:
        pass


def refresh_token(account, timeout=90):
    """Start the CLI briefly for that account to trigger an access token refresh.
    Returns (ok, reason).

    Success is decided by **whether expiresAt actually moved forward**, not by the child's
    exit code: an account that hit its usage limit still refreshes while the call fails.

    Reading and writing the cooldown happens under a per-account lock, so a keepalive run
    and a quota() call cannot start rotating at the same time. The cooldown is only recorded
    once the CLI really started - a failed launch should not push the next attempt 30 minutes
    out. An account whose credentials already expired ignores the cooldown and tries at once.
    """
    if DRY:
        return False, "--dry, so the refresh call was skipped"
    if os.environ.get(REFRESH_GUARD_ENV):
        return False, "cannot be called again inside a refresh run (recursion guard)"
    acc = ACCOUNTS.get(account)
    if not acc:
        return False, f"unknown account: {account}"
    before, tok_before = _token_fingerprint(account)
    if not before:
        return False, "cannot read the credentials - that account needs to log in again"

    lock_f, got_lock, lock_reason = _acquire_refresh_lock(account)
    if not got_lock:
        return False, lock_reason

    try:
        already_expired = (before / 1000 - time.time()) <= 0
        if not already_expired and _refresh_recent(account):
            return False, f"the previous refresh was less than {REFRESH_COOLDOWN}s ago, skipping"
        binary = _claude_bin()
        if not binary:
            return False, "could not find the claude executable"
        return _do_refresh(account, acc, before, tok_before, binary, timeout)
    finally:
        _release_refresh_lock(lock_f)


def _do_refresh(account, acc, before, tok_before, binary, timeout):
    """The actual launch, wait and verdict. Only called while holding the lock."""
    env = dict(os.environ)
    # For an account on the default config directory (~/.claude), CLAUDE_CONFIG_DIR is
    # **not** set. With it set the CLI looks for the login in `~/.claude/.claude.json`,
    # which on some machines is an empty shell without oauthAccount and the run ends
    # immediately with `Not logged in`. The real login for the default directory lives in
    # `~/.claude.json`.
    if acc["config_dir"] == DEFAULT_CONFIG_DIR:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = acc["config_dir"]
    env[REFRESH_GUARD_ENV] = "1"
    cwd = os.path.join(acc["config_dir"], "work")  # neutral cwd, keeps project CLAUDE.md out
    try:
        os.makedirs(cwd, exist_ok=True)
    except Exception:
        cwd = None

    # Minimal tool schema and model. The process is killed before the answer arrives anyway.
    cmd = [binary, "-p", "Reply with 1 and nothing else.", "--model", "sonnet",
           "--effort", "low", "--tools", "none"]
    kw = {}
    if os.name == "posix":
        kw["start_new_session"] = True  # so the whole process group can be killed
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, cwd=cwd, **kw)
    except Exception as e:
        return False, f"failed to start the CLI: {type(e).__name__}: {e}"

    # The CLI did start, so from here on the cooldown is recorded. A failed launch (above)
    # deliberately does not record it.
    _refresh_mark(account)

    def _changed():
        exp, tok = _token_fingerprint(account)
        # expiresAt alone is not enough - the access token itself must change.
        return bool(exp and tok and exp > before and tok != tok_before)

    ok = False
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            time.sleep(0.3)
            if _changed():
                # Killing right away can interrupt the CLI while it is still writing the
                # credentials. Wait a moment and read again to confirm.
                time.sleep(REFRESH_SETTLE)
                ok = _changed()
                break
            if proc.poll() is not None:
                # Even when the child exited on its own, the credential write happens just
                # before exit and may not be visible yet (an immediate verdict here produced
                # false failures on real refreshes). Re-check at a short interval for
                # REFRESH_SETTLE and accept as soon as it turns true.
                settle_deadline = time.time() + REFRESH_SETTLE
                while True:
                    if _changed():
                        ok = True
                        break
                    if time.time() >= settle_deadline:
                        ok = False
                        break
                    time.sleep(0.2)
                break
    finally:
        _kill(proc)
    if ok:
        return True, f"refreshed in {time.time() - t0:.1f}s"
    return False, "the CLI ran but the token was not refreshed - a new login may be needed"


def _kill(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            # On Windows `claude` is a shell shim that spawns node, so terminate() can
            # leave the child behind. Kill the whole tree.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass


def fetch(account):
    """Query one account for real. Raises on failure."""
    acc = ACCOUNTS[account]
    oauth = _read_token(acc["creds"])
    tok = oauth.get("accessToken")
    if not tok:
        raise TokenMissing("no token - that account was never logged in")

    # Querying with an expired token returns 401. Filtering it out early saves the call and
    # lets the caller tell "limit reached" apart from "token expired" via a distinct error.
    exp = oauth.get("expiresAt")
    if exp:
        left = exp / 1000 - time.time()
        if left <= 300:
            when = datetime.fromtimestamp(exp / 1000, LOCAL_TZ).strftime("%m-%d %H:%M")
            raise TokenExpired(f"access token expired (as of {when})")

    req = urllib.request.Request(OAUTH_URL, headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": f"claude-code/{_cli_version()}",
    })
    r = json.load(urllib.request.urlopen(req, timeout=20))

    five, seven = r.get("five_hour") or {}, r.get("seven_day") or {}
    sev = {}
    scoped, scoped_reset = {}, {}
    for l in r.get("limits") or []:
        kind = l.get("kind")
        if kind in ("session", "weekly_all"):
            sev[kind] = l.get("severity")
        elif kind == "weekly_scoped" and l.get("scope"):
            # Per-model weekly limit. Only sent on the max plan, absent on pro and free.
            name = ((l["scope"].get("model") or {}).get("display_name")) or "scoped"
            scoped[name] = l.get("percent")
            scoped_reset[name] = _to_local(l.get("resets_at"))

    xu = r.get("extra_usage") or {}
    spend = r.get("spend") or {}
    used, limit = spend.get("used") or {}, spend.get("limit") or {}

    def _usd(m):
        if not m:
            return None
        return (m.get("amount_minor") or 0) / (10 ** (m.get("exponent") or 2))

    return {
        "account": account,
        "label": acc["label"],
        "email": _read_email(acc["meta"]),
        "plan": oauth.get("subscriptionType"),  # max / pro / free
        "session_pct": five.get("utilization"),
        "session_reset": _to_local(five.get("resets_at")),
        "session_severity": sev.get("session"),
        "weekly_pct": seven.get("utilization"),
        "weekly_reset": _to_local(seven.get("resets_at")),
        "weekly_severity": sev.get("weekly_all"),
        "scoped": scoped,
        "scoped_reset": scoped_reset,
        "extra": {
            "enabled": bool(spend.get("enabled") or xu.get("is_enabled")),
            "used_usd": _usd(used),
            "limit_usd": _usd(limit),
        },
        "ts": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "cached": False,
    }


def quota(account, ttl=CACHE_TTL, force=False, auto_refresh=True):
    """Return the quota dict. Never raises - failures come back in the `error` key.

    Callers need to inspect `error` to decide on a fallback, so this function never throws.
    An expired or rejected token triggers one refresh and exactly one retry when
    auto_refresh is on.
    """
    if account not in ACCOUNTS:
        return {"account": account, "error": f"unknown account: {account}", "error_kind": "config"}
    if not force and ttl > 0:
        c = _load_cache(account, ttl)
        if c:
            return c

    def _retry(kind, msg):
        """On expiry or rejection, trigger a refresh and query exactly once more."""
        if not auto_refresh:
            return _err(account, msg, kind)
        ok, why = refresh_token(account)
        if ok:
            return quota(account, ttl=0, force=True, auto_refresh=False)
        return _err(account, f"{msg} / automatic refresh failed: {why}", kind)

    try:
        d = fetch(account)
        _save_cache(account, d)
        return d
    except TokenExpired as e:
        # Not cached - a retry right after the refresh must not hit a stale failure.
        return _retry("auth_expired", str(e))
    except TokenMissing as e:
        return _err(account, str(e), "auth_missing")
    except FileNotFoundError:
        return _err(account, "no configuration - this account is not logged in", "auth_missing")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # Even if the clock says it is valid, a server rejection means a dead token.
            return _retry("auth_rejected", f"query rejected ({e.code})")
        return _err(account, f"HTTP {e.code}", "http")
    except urllib.error.URLError as e:
        return _err(account, f"network error: {e.reason}", "network")
    except Exception as e:
        return _err(account, f"{type(e).__name__}: {e}", "unknown")


def _err(account, msg, kind="unknown"):
    """Failures are dicts too. error_kind separates "limit reached" from "auth problem".
    kind: auth_expired / auth_missing / auth_rejected / network / http / config / unknown"""
    acc = ACCOUNTS.get(account, {})
    return {
        "account": account,
        "label": acc.get("label", account),
        "email": _read_email(acc["meta"]) if acc.get("meta") else None,
        "error": msg,
        "error_kind": kind,
        "ts": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "cached": False,
    }


# -- display helpers ---------------------------------------------------------------------
def fmt_reset(iso):
    """Render a reset time as "today 19:59 (in 20 min)"."""
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return str(iso)
    now = datetime.now(LOCAL_TZ)
    left = dt - now
    mins = int(left.total_seconds() // 60)
    if mins < 0:
        rel = "already passed"
    elif mins < 60:
        rel = f"in {mins} min"
    elif mins < 60 * 24:
        rel = f"in {mins // 60}h {mins % 60}m"
    else:
        rel = f"in {mins // (60 * 24)}d {(mins % (60 * 24)) // 60}h"
    day = "today" if dt.date() == now.date() else dt.strftime("%b %d")
    return f"{day} {dt.strftime('%H:%M')} ({rel})"


def pct_text(v):
    if v is None:
        return "unknown"
    return f"{v:.0f}%"


def summary_line(d):
    """One line per account."""
    who = f"{d.get('label')}({d.get('email') or '?'})"
    if d.get("error"):
        return f"{who}: query failed - {d['error']}"
    plan = (d.get("plan") or "?").upper()
    s = f"{who} [{plan}] 5h {pct_text(d.get('session_pct'))}, weekly {pct_text(d.get('weekly_pct'))}"
    if d.get("scoped"):
        s += " (" + ", ".join(f"{k} {pct_text(v)}" for k, v in d["scoped"].items()) + ")"
    return s


def main():
    ap = argparse.ArgumentParser(description="Read Claude account usage limits")
    ap.add_argument("--dry", action="store_true",
                      help="only print what would happen, no side effects")
    ap.add_argument("--account", choices=account_names() + ["all", "both"], default="all",
                      help="account to query (default: all)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--ttl", type=int, default=CACHE_TTL, help=f"cache lifetime in seconds (default {CACHE_TTL})")
    ap.add_argument("--no-refresh", action="store_true",
                      help="never start the CLI to refresh an expired token (query only)")
    a = ap.parse_args()

    names = account_names() if a.account in ("all", "both") else [a.account]
    out = {n: quota(n, ttl=a.ttl, force=a.refresh, auto_refresh=not a.no_refresh) for n in names}

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    for n in names:
        d = out[n]
        print(summary_line(d))
        if not d.get("error"):
            print(f"    5h reset:     {fmt_reset(d.get('session_reset'))}")
            print(f"    weekly reset: {fmt_reset(d.get('weekly_reset'))}")
            if d.get("cached"):
                print(f"    (cached {d.get('ts')})")
        exp_dt, left = token_expiry(n)
        if exp_dt is not None:
            print(f"    token expiry: {fmt_reset(exp_dt.isoformat(timespec='seconds'))}"
                  + ("  <- expired (automatic refresh failed). That account may need to log in again"
                     if left is not None and left <= 300 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
