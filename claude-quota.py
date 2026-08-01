#!/usr/bin/env python3
"""CLI entry point for the quota reader.

All logic lives in `claude_quota.py`; this file only exists so the tool can be called with
the hyphenated name people expect on the command line. Keeping one module and one shim
avoids two copies of the same code and works on filesystems without symlinks (Windows).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_quota  # noqa: E402  (path must be set up first)

if __name__ == "__main__":
    sys.exit(claude_quota.main())
