#!/usr/bin/env python3
"""Importing must not touch the caller's argv, and refresh locking must fail closed.

Both defects are load-bearing rather than cosmetic:

* the module used to read `--dry` from `sys.argv` at import time and then delete it, so a
  host CLI importing this library lost its own flag before its parser ran;
* `_acquire_refresh_lock` used to pass through unlocked wherever `fcntl` was missing
  (Windows). OAuth refresh-token rotation is destructive — two concurrent rotations
  invalidate one token, which ends in 401 and the credentials being deleted.

Nothing here touches the network or real credentials: the refresh itself is stubbed and
every assertion is about which code path ran.
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
FAILURES = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (f" - {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def run_source(code, env=None, argv=()):
    """Run a snippet in a subprocess with ROOT importable. Returns (rc, stdout, stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as t:
        t.write(code)
        path = t.name
    try:
        e = dict(os.environ)
        e["PYTHONPATH"] = ROOT + os.pathsep + e.get("PYTHONPATH", "")
        e.update(env or {})
        p = subprocess.run([PY, path, *argv], capture_output=True, text=True, env=e)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    finally:
        os.unlink(path)


print("import does not mutate the caller's argv")
rc, out, err = run_source("""
import sys
sys.argv = ['host', '--dry', 'sentinel', '--json']
before = list(sys.argv)
import claude_quota
print('SAME' if sys.argv == before else 'CHANGED:' + repr(sys.argv))
print('DRY=', claude_quota.DRY)
""")
check("argv is byte-for-byte unchanged", "SAME" in out, out + err)
check("DRY is False right after import", "DRY= False" in out, out + err)

print("direct CLI --dry still suppresses side effects")
rc, out, err = run_source("""
import sys
import claude_quota as q
sys.argv = ['claude-quota.py', '--dry', '--json', '--no-refresh']
q.quota = lambda *a, **k: {'account': 'main'}
q.token_expiry = lambda a: (None, None)
q.main()
print('DRY_AFTER=', q.DRY)
print('SAVE=', q._save_cache('main', {'x': 1}))
print('REFRESH=', q.refresh_token('main'))
""")
check("main() sets DRY from the parsed args", "DRY_AFTER= True" in out, out + err)
check("--dry skips the cache write", "SAVE= None" in out, out + err)
check("--dry skips the refresh call", "REFRESH= (False," in out, out + err)

print("a second lock on the same account fails (POSIX)")
rc, out, err = run_source("""
import claude_quota as q
f1, ok1, r1 = q._acquire_refresh_lock('main')
f2, ok2, r2 = q._acquire_refresh_lock('main')
print('FIRST=', ok1, '| SECOND=', ok2, '|', r2)
q._release_refresh_lock(f1)
f3, ok3, r3 = q._acquire_refresh_lock('main')
print('AFTER_RELEASE=', ok3)
q._release_refresh_lock(f3)
""")
check("first acquisition succeeds", "FIRST= True" in out, out + err)
check("second acquisition fails", "SECOND= False" in out, out + err)
check("the lock is released again", "AFTER_RELEASE= True" in out, out + err)

print("a missing or broken lock backend prevents the refresh (fail closed)")
rc, out, err = run_source("""
import claude_quota as q
def must_not_run(*a, **k):
    raise AssertionError('the refresh started')
q._do_refresh = must_not_run
q._file_lock_mod = lambda: None
print('NO_MODULE=', q.refresh_token('main'))
class Broken:
    IS_WIN = False
    @staticmethod
    def try_lock(f):
        raise OSError('backend broken')
    @staticmethod
    def unlock(f):
        pass
q._file_lock_mod = lambda: Broken
print('BROKEN=', q.refresh_token('main'))
""")
check("no lock module means no refresh", "NO_MODULE= (False," in out, out + err)
check("a raising lock backend means no refresh", "BROKEN= (False," in out, out + err)
check("_do_refresh never ran", "the refresh started" not in (out + err), out + err)

print("the Windows backend is exercised the same way")
rc, out, err = run_source("""
import claude_quota as q
state = {'n': 0}
class Win:
    IS_WIN = True
    @staticmethod
    def try_lock(f):
        state['n'] += 1
        return state['n'] == 1
    @staticmethod
    def unlock(f):
        pass
q._file_lock_mod = lambda: Win
f1, ok1, r1 = q._acquire_refresh_lock('main')
f2, ok2, r2 = q._acquire_refresh_lock('main')
print('WIN_FIRST=', ok1, '| WIN_SECOND=', ok2, '|', r2)
""")
check("Windows backend: first acquisition succeeds", "WIN_FIRST= True" in out, out + err)
check("Windows backend: second acquisition fails", "WIN_SECOND= False" in out, out + err)

print("keepalive refuses to run without a lock backend")
with tempfile.TemporaryDirectory() as td:
    # Copy only what keepalive imports, leaving claude_file_lock out of every search path.
    isolated = os.path.join(td, "bin")
    os.makedirs(isolated)
    for name in ("argv_common.py", "claude_quota.py", "claude-token-keepalive.py"):
        with open(os.path.join(ROOT, name), encoding="utf-8") as src, \
                open(os.path.join(isolated, name), "w", encoding="utf-8") as dst:
            dst.write(src.read())
    env = dict(os.environ)
    env["HOME"] = td
    env.pop("PYTHONPATH", None)
    p = subprocess.run([PY, os.path.join(isolated, "claude-token-keepalive.py")],
                       capture_output=True, text=True, env=env)
    check("exits 1 and says why",
          p.returncode == 1 and "claude_file_lock" in p.stdout,
          f"rc={p.returncode} out={p.stdout[:200]}")

    # Positive control: the same isolated tree passes the lock stage once the module is there.
    with open(os.path.join(ROOT, "claude_file_lock.py"), encoding="utf-8") as src, \
            open(os.path.join(isolated, "claude_file_lock.py"), "w", encoding="utf-8") as dst:
        dst.write(src.read())
    p2 = subprocess.run([PY, os.path.join(isolated, "claude-token-keepalive.py")],
                        capture_output=True, text=True, env=env)
    check("positive control: with the module it gets past the lock",
          "claude_file_lock" not in p2.stdout, f"rc={p2.returncode} out={p2.stdout[:200]}")

print()
print(f"{len(FAILURES)} failure(s)" + ((": " + ", ".join(FAILURES)) if FAILURES else ""))
sys.exit(1 if FAILURES else 0)
