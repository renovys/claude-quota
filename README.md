# claude-quota

Read your Claude Code usage limits from the **official OAuth usage endpoint**, for one or
several accounts, from the command line.

Most similar tools parse the local `~/.claude/projects/**/*.jsonl` transcripts and add up
token counts to *estimate* how much of your window you have burned. That estimate drifts:
it cannot see usage from other machines, it does not know how the plan weighs cache reads,
and it has no idea when the window actually resets. This tool asks the same endpoint the
Claude Code CLI itself uses (`GET /api/oauth/usage`), so the numbers and reset times are
the ones Anthropic is actually counting.

Reading the endpoint does not consume any of the usage it reports.

## What you get

- 5-hour window and weekly window utilisation, plus the exact reset timestamps
- per-model weekly windows when your plan reports them (max plans do)
- extra-usage (spend) status when enabled on the account
- several accounts side by side, separated by `CLAUDE_CONFIG_DIR`
- JSON output for scripting, a 60 second cache, and machine readable `error_kind` values
  so a caller can tell "limit reached" apart from "token expired"
- optional keepalive that rotates access tokens before they expire, for unattended hosts

## Requirements

Python 3.8 or newer, standard library only. A logged-in Claude Code CLI (`claude`) on the
machine, which is what creates and refreshes the credentials this tool reads.

## Install

```
git clone https://github.com/renovys/claude-quota.git
cd claude-quota
python3 quota-all.py
```

Nothing to build and nothing to configure. The scripts resolve their siblings relative to
their own location, so they can be symlinked onto your `PATH` or called by absolute path
from cron.

## Usage

```
quota-all.py                  bar chart for every configured account
quota-all.py --refresh        ignore the 60 second cache
quota-all.py --json           raw data as JSON
quota-all.py --no-refresh     never start the CLI, even on an expired token

claude-quota.py               one line per account, plus reset times
claude-quota.py --json        machine readable output
claude-quota.py --account sub a single account
```

Every script accepts `-h/--help`, accepts `--dry`, and rejects unknown `--options` with
exit code 2.

### Example output

Values below are made up.

```
Usage limits  (as of 03/14 21:07)

Claude [main]  [MAX]  you@example.com
  5-hour           🟢 ████················  21.0%   resets today 23:15 (2h 8m)
  weekly           🟡 ███████████████·····  74.0%   resets 03/17 09:00 (2d 11h)
  weekly/Opus      🔴 ██████████████████··  91.0%   resets 03/17 09:00 (2d 11h)

Claude [sub]  [PRO]  other@example.com
  5-hour           🟢 █···················   4.0%   resets today 22:40 (1h 33m)
  weekly           🟢 █████████···········  45.0%   resets 03/16 00:00 (1d 2h)
```

### As a library

```python
from claude_quota import quota

d = quota("main")            # never raises; failures come back in d["error"]
print(d["session_pct"], d["weekly_reset"], d.get("error_kind"))
```

`quota()` returns a dict with `session_pct`, `session_reset`, `weekly_pct`,
`weekly_reset`, `scoped`, `scoped_reset`, `plan`, `extra`, `ts` and `cached`. On failure it
returns `error` plus `error_kind` (`auth_expired`, `auth_missing`, `auth_rejected`,
`network`, `http`, `config`, `unknown`) instead of raising.

## Multiple accounts

Claude Code separates accounts through the `CLAUDE_CONFIG_DIR` environment variable: point
it at a different directory and the CLI logs in, caches and stores credentials there.

```
CLAUDE_CONFIG_DIR=~/.claude-sub claude    # log in once as the second account
```

By default this tool reads `~/.claude` as `main` and, when the directory exists,
`~/.claude-sub` as `sub`. Override the whole table with a comma separated list:

```
export CLAUDE_QUOTA_ACCOUNTS="work=~/.claude,personal=~/.claude-alt"
```

Note that the default config directory keeps its account metadata in `~/.claude.json`,
while every other config directory keeps it inside the directory itself. The tool already
accounts for that difference.

Cache and state files live in `~/.claude-quota/`, or in `CLAUDE_QUOTA_CACHE_DIR` if set.

## Token keepalive

The OAuth access token lasts roughly eight hours and only the Claude Code CLI can rotate
it. An account you do not use for a while therefore ends up with an expired token, and the
usage query starts failing. `claude-quota.py` handles that on demand: on expiry or a
401/403 it starts the CLI briefly with that config directory, kills it as soon as new
credentials appear, and retries the query once.

For unattended machines, `claude-token-keepalive.py` does the same thing ahead of time:

```
claude-token-keepalive.py --dry               show time left, refresh nothing
claude-token-keepalive.py --threshold 0.5     refresh below 30 minutes left
claude-token-keepalive.py --account sub       one account only
```

```
# hourly, from cron
0 * * * * /path/to/claude-token-keepalive.py >> /tmp/claude-keepalive.log 2>&1
```

To be told about real failures, point it at any command you like. The message is appended
as one argument:

```
export CLAUDE_QUOTA_NOTIFY_CMD="/usr/local/bin/my-notifier"
```

Failures are reported at most once per account and reason every six hours; successes are
never reported.

Two cautions. A refresh is a **refresh token rotation**: the old token dies immediately, so
rotating repeatedly in a short window can invalidate the login. The tool keeps a 30 minute
cooldown, takes a per-account lock (fail closed - no lock, no refresh), and waits two
seconds after the credentials change before killing the CLI, precisely to avoid that. And the refresh is triggered by starting
`claude -p` with a trivial prompt, which is killed before the model answers; it normally
costs no meaningful usage, but it is not literally zero work.

## Cross-platform notes

- **Linux/BSD**: credentials in `<config dir>/.credentials.json`. Nothing special.
- **macOS**: there is no credentials file. The same JSON lives in the login keychain under
  `Claude Code-credentials`, with the first 8 hex characters of the sha256 of the config
  directory's absolute path appended for non-default directories. The tool reads it through
  `security find-generic-password`, which may prompt for keychain access the first time.
- **Windows**: `fcntl` does not exist, so the locks go through `msvcrt.locking` instead
  (`claude_file_lock.py` picks the backend). Refresh locking **fails closed** everywhere: if
  the lock cannot be taken, the refresh does not start, because a concurrent refresh-token
  rotation can invalidate the login. The process tree is killed with `taskkill /F /T`. The console's default code page is often not UTF-8, so the
  scripts pin UTF-8 on stdout and force `PYTHONIOENCODING=utf-8` on the child process.
  Without that, non-ASCII output from a subprocess decodes as the ANSI code page (cp949 on
  a Korean install, for instance) and the JSON read fails.

## Limits and disclaimer

This is an **unofficial** tool and is not affiliated with or endorsed by Anthropic.

`/api/oauth/usage` is not a documented public API. It can change or disappear without
notice, and the response fields this tool reads (`five_hour`, `seven_day`, `limits`,
`spend`) may be renamed or restructured at any time. The tool never writes to your
credentials; it reads them and lets the official CLI do any rotation. Even so, it is a
third-party tool touching an authenticated endpoint, so use it at your own risk.

Nothing is uploaded anywhere: the only network call goes to `api.anthropic.com`, and the
access token is never logged or stored (only a truncated hash of it is used, in memory, to
detect a rotation).

## Tests

```
python3 test/run_tests.py
```

Byte-compiles every source file and checks the argument-handling convention: `--help`
exits 0, an unknown option exits 2, and every flag the body interprets is present in the
guard's allow-set. The checks make no network calls and do not read your credentials.

## License

MIT. See [LICENSE](LICENSE).
