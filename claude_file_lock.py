"""Cross-platform advisory file locking used by the refresh paths.

POSIX uses fcntl.flock, Windows uses msvcrt.locking. Both are non-blocking: the caller
decides what to do when the lock is already held. Callers that rotate credentials must
treat "cannot lock" as "do not proceed" — see _acquire_refresh_lock in claude_quota.py.
"""
import hashlib
import os
import re
import time


LOCK_DIR = os.path.join(os.path.expanduser("~"), ".cache", "claude-file-locks")
IS_WIN = os.name == "nt"

if IS_WIN:
    import msvcrt
else:
    import fcntl


def lock_path(target):
    """Return the normalized shared lock file path for a target file."""
    real = os.path.realpath(os.path.expanduser(target))
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(real))
    os.makedirs(LOCK_DIR, exist_ok=True)
    try:
        os.chmod(LOCK_DIR, 0o700)
    except OSError:
        pass
    return os.path.join(LOCK_DIR, "{}.{}.lock".format(base, digest))


def try_lock(fh):
    """Try to take a non-blocking exclusive lock on an open file handle."""
    try:
        if IS_WIN:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(fh):
    """Release the lock. On POSIX closing is enough; Windows needs an explicit unlock."""
    if IS_WIN:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def _alive(pid):
    """Is that pid running now? If it cannot be decided, answer True (conservative).

    Windows is never probed: os.kill(pid, 0) there is not a liveness probe but a
    CTRL_C_EVENT (signal.CTRL_C_EVENT == 0 on Windows Python). A corrupted marker holding
    pid=0 would send Ctrl+C to the whole console group.
    """
    if pid <= 0 or IS_WIN:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # someone else's process - alive
    except OSError:
        return True          # cannot tell - assume held
    return True


def read_owner(lock_file):
    """Return the owner marker, or None when it is missing, empty or stale.

    A process killed by SIGKILL/OOM/power loss never clears its marker. The kernel has
    already released the lock itself, so a stale marker would report a months-dead pid as
    the holder forever. Reading therefore checks liveness and drops dead markers.
    """
    try:
        with open(lock_file + ".owner", encoding="utf-8") as owner:
            text = owner.read().strip() or None
    except OSError:
        return None
    if not text:
        return None
    match = re.search(r"\bpid=(\d+)", text)
    if match and not _alive(int(match.group(1))):
        clear_owner(lock_file)
        return None
    return text


def write_owner(lock_file, cmd):
    """Write this process's owner marker. Failing to do so does not drop the lock."""
    try:
        with open(lock_file + ".owner", "w", encoding="utf-8") as owner:
            owner.write("pid={} cmd={} at={}\n".format(
                os.getpid(), cmd, time.strftime("%Y-%m-%d %H:%M:%S")))
    except OSError:
        pass


def clear_owner(lock_file):
    """Remove the owner marker. Missing or unremovable is fine."""
    try:
        os.remove(lock_file + ".owner")
    except OSError:
        pass
