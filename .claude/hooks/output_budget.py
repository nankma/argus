#!/usr/bin/env python
"""No-op. The real output-budget guard is global, at
~/.claude/hooks/output_budget.py, registered in ~/.claude/settings.json --
runaway tool output is an account-level problem, not a repo-level one.

This file only exists because a session that loaded the old project-scoped
registration keeps calling this path until its config is reloaded, and a
hook that errors on every tool call is worse than no hook. It deliberately
does NOT delegate to the global script: both registrations can be live at
once, and counting the same bytes twice would trip the budget at half the
measured threshold.

Safe to delete once no session is running with the old config.
"""

import sys

sys.stdin.read()
