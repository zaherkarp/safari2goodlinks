"""macOS notification for the scheduled run.

Uses terminal-notifier if it is installed (brew install terminal-notifier),
because only it can open the triage file when the notification is
clicked. Otherwise falls back to AppleScript's `display notification`,
which shows the message but does nothing useful on click. Off macOS (the
tests) it prints the message instead.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.parse


def notify(title: str, message: str, open_path: str = None) -> str:
    """Post a notification. Returns which method was used, for logging."""
    tn = shutil.which("terminal-notifier")
    if tn:
        cmd = [tn, "-title", title, "-message", message, "-group", "stg2gl-triage"]
        if open_path:
            cmd += ["-open", "file://" + urllib.parse.quote(open_path)]
        subprocess.run(cmd, capture_output=True, timeout=15)
        return "terminal-notifier"
    if shutil.which("osascript"):
        # Pass text as argv, never spliced into the script, so a tab title
        # in the message cannot inject AppleScript.
        subprocess.run(
            ["osascript",
             "-e", "on run argv",
             "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
             "-e", "end run",
             title, message],
            capture_output=True, timeout=15,
        )
        return "osascript"
    print(f"[notify] {title}: {message}", file=sys.stderr)
    return "stderr"
