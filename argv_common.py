"""Shared CLI argument convention: help, dry-run and rejection of unknown flags.

Two lessons behind it:
 1. The `"--dry" in sys.argv` idiom silently ignores typos (`--dry-run`, `--drny`) and runs
    the real thing anyway, which defeats the whole point of a dry run.
 2. A script without help handling treats `"--help"` as ordinary input, so the flag ends up
    being processed as data. The common cause is an unknown option leaking into the body.

Convention (all scripts in this repository):
 - `-h` / `--help`     -> print usage, exit 0
 - `--dry`             -> describe what would happen, no side effects
                          (read-only scripts just say so and run normally)
 - unknown `--` option -> "[<name>] unknown option: <opt>" + "see --help", exit 2

Positional arguments, flag values, single-dash options (-x) and anything after `--` are
left untouched. Escape hatch: set CLAUDE_ARGV_LAX=1 to let a new flag through temporarily.

Errors inside the check itself fail open (the check is skipped) so that a helper bug never
kills a cron job. A decided "unknown option", however, is an intentional exit 2 and is not
swallowed.
"""
import os
import sys

LAX_ENV = "CLAUDE_ARGV_LAX"
HELP_FLAGS = ("-h", "--help")
DRY_FLAGS = ("--dry",)


def prog_name(prog=None):
    """Name used in error messages: the basename without its extension."""
    if prog:
        return prog
    try:
        base = os.path.basename(sys.argv[0] or "script")
    except Exception:
        return "script"
    for ext in (".py", ".sh"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base or "script"


def reject_unknown(bad, prog=None):
    """Reject unknown options in the conventional format and exit 2."""
    name = prog_name(prog)
    for opt in bad:
        sys.stderr.write("[%s] unknown option: %s\n" % (name, opt))
    sys.stderr.write("[%s] see --help\n" % name)
    sys.exit(2)


def cli_guard(usage, known=(), argv=None, prog=None, dry_stop=None, dry_readonly=False):
    """CLI convention gate. Call it once at the top of `__main__`.

    - if `-h`/`--help` is present, print usage and exit 0 (checked before unknown options,
      so help is always reachable)
    - if a `--` option is not in `known`, reject it in the conventional format and exit 2
    - returns whether `--dry` was given (bool)

    `-h`, `--help` and `--dry` are added to `known` automatically, so only pass the flags
    that are specific to the script.

    Two shortcuts for scripts that do not implement dry-run handling themselves:
    - `dry_stop="<what it does>"`: the script has side effects but no step-by-step preview.
      With `--dry` it does **nothing**, prints what the script would do, and exits 0.
      Merely accepting the flag would produce a "dry in name only" run, and stopping is
      safer than silently performing a real deletion or send.
    - `dry_readonly=True`: no side effects at all. Print one note and continue.
    """
    seq = list(sys.argv[1:] if argv is None else argv)

    # Help comes before the unknown-option check, so whoever made the typo can read it.
    for tok in seq:
        if tok == "--":
            break
        if tok in HELP_FLAGS:
            sys.stdout.write(usage if usage.endswith("\n") else usage + "\n")
            sys.exit(0)

    allow = set(known) | set(HELP_FLAGS) | set(DRY_FLAGS)
    if os.environ.get(LAX_ENV) != "1":
        try:
            bad = unknown_flags(allow, seq)
        except Exception:
            bad = []        # a failing check falls back to the previous behaviour
        if bad:
            reject_unknown(bad, prog)

    dry = False
    for tok in seq:
        if tok == "--":
            break
        if tok in DRY_FLAGS:
            dry = True
            break

    if dry and dry_stop:
        name = prog_name(prog)
        sys.stdout.write(
            "[%s] --dry: no step-by-step preview exists yet, so nothing was done.\n"
            "[%s] what this script does: %s\n"
            "[%s] run it without --dry to do it for real. See --help for details.\n"
            % (name, name, dry_stop, name))
        sys.exit(0)
    if dry and dry_readonly:
        sys.stdout.write("[%s] read-only, so --dry behaves the same - running normally\n"
                         % prog_name(prog))

    # Strip `--dry` from sys.argv. Accepting it but leaving it in place makes it **leak into
    # the positional arguments**: a date argument becomes "--dry", a filename becomes "--dry",
    # and argparse reports it as unrecognized after a subcommand and exits 2.
    # Code that reads sys.argv at module import time runs before this point and must
    # handle the flag on its own.
    if dry and argv is None:
        cut = len(sys.argv)
        for i, tok in enumerate(sys.argv[1:], start=1):
            if tok == "--":
                cut = i
                break
        head = [a for a in sys.argv[1:cut] if a not in DRY_FLAGS]
        sys.argv[1:] = head + sys.argv[cut:]

    return dry


def unknown_flags(known, argv=None):
    """Return the `--` flags in argv that are not in `known` (no side effects, for tests)."""
    seq = list(sys.argv[1:] if argv is None else argv)
    allow = set(known)
    out = []
    for tok in seq:
        if tok == "--":
            break
        if tok.startswith("--"):
            base = tok.split("=", 1)[0]
            if base not in allow:
                out.append(tok)
    return out


def check_flags(known, argv=None):
    """Exit 2 if argv holds a `--` flag outside `known`. Skipped when LAX_ENV=1."""
    if os.environ.get(LAX_ENV) == "1":
        return
    try:
        bad = unknown_flags(known, argv)
    except Exception:
        return          # a failing check falls back to the previous behaviour
    if bad:
        name = prog_name()
        sys.stderr.write(
            "[%s] known flags: %s (set %s=1 to bypass if this is not a typo)\n"
            % (name, " ".join(sorted(set(known))) or "(none)", LAX_ENV))
        reject_unknown(bad)
