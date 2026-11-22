"""Run `stg2gl-triage auto` on a schedule with launchd (option 1).

launchd is macOS's own job scheduler. A "launch agent" is a plist in
~/Library/LaunchAgents that runs a command as you, while you are logged
in. If the Mac is asleep at the scheduled time, launchd runs the job when
it wakes; if the Mac was shut down, that run is skipped.

Permissions are the sharp edge. A job started by launchd is not started
by Terminal, so it does not inherit Terminal's permissions. macOS asks
about the python3 binary itself instead:
  - Automation ("python3 wants to control Safari"): answered by a dialog
    on the first run. `schedule run-now` while you are at the Mac gets
    that dialog out of the way.
  - Full Disk Access (for Tab Groups and iCloud Tabs): must be granted by
    hand to the python3 path that `schedule install` prints.
UNVERIFIED: exactly which binary macOS attributes the request to can
vary by macOS version. If the scheduled run logs permission errors while
manual runs work, that is the cause.
"""

from __future__ import annotations

import os
import plistlib
import subprocess

LABEL = "com.stg2gl.triage"
# Homebrew paths first so terminal-notifier is found; launchd's default
# PATH is only /usr/bin:/bin:/usr/sbin:/sbin.
JOB_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# launchd weekday numbers: 0 (or 7) is Sunday.
WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


class ScheduleError(Exception):
    pass


def plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def log_path():
    return os.path.expanduser("~/Library/Logs/stg2gl-triage.log")


def _parse_time(hhmm: str):
    try:
        h, m = hhmm.split(":")
        h, m = int(h), int(m)
    except ValueError:
        raise ScheduleError(f"time must be HH:MM, got {hhmm!r}")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ScheduleError(f"time out of range: {hhmm!r}")
    return h, m


def parse_when(daily: str = None, weekly: str = None):
    """Return launchd StartCalendarInterval entries.

    daily="18:30"            every day at 18:30
    weekly="sun 09:00"       Sundays at 09:00
    weekly="mon,thu 18:30"   Mondays and Thursdays at 18:30
    """
    if bool(daily) == bool(weekly):
        raise ScheduleError("give exactly one of --daily HH:MM or --weekly 'DAY[,DAY] HH:MM'")
    if daily:
        h, m = _parse_time(daily)
        return [{"Hour": h, "Minute": m}]
    try:
        days, hhmm = weekly.split()
    except ValueError:
        raise ScheduleError(f"--weekly must look like 'sun 09:00', got {weekly!r}")
    h, m = _parse_time(hhmm)
    out = []
    for d in days.lower().split(","):
        if d[:3] not in WEEKDAYS:
            raise ScheduleError(f"unknown day {d!r}; use sun mon tue wed thu fri sat")
        out.append({"Weekday": WEEKDAYS[d[:3]], "Hour": h, "Minute": m})
    return out


def build_plist(python: str, script: str, rules: str, when) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [python, script, "--rules", rules, "auto"],
        "StartCalendarInterval": when,
        "StandardOutPath": log_path(),
        "StandardErrorPath": log_path(),
        "EnvironmentVariables": {"PATH": JOB_PATH},
        "RunAtLoad": False,
        # Low priority: this is background housekeeping.
        "ProcessType": "Background",
    }


def _launchctl(*args, check=True):
    res = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise ScheduleError(f"launchctl {' '.join(args)} failed: {res.stderr.strip() or res.returncode}")
    return res


def _domain():
    return f"gui/{os.getuid()}"


def install(python: str, script: str, rules: str, when) -> str:
    """Write the plist and (re)load it. Returns the plist path."""
    path = plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.dirname(log_path()), exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(build_plist(python, script, rules, when), f)
    # bootout fails if the job is not loaded yet; that is fine.
    _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)
    _launchctl("bootstrap", _domain(), path)
    return path


def remove() -> bool:
    """Unload and delete the plist. Returns False if nothing was installed."""
    path = plist_path()
    _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def run_now():
    """Start the job immediately, as launchd would (same permissions)."""
    _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")


def describe() -> str:
    path = plist_path()
    if not os.path.exists(path):
        return "No schedule installed."
    with open(path, "rb") as f:
        pl = plistlib.load(f)
    names = {v: k for k, v in WEEKDAYS.items()}
    parts = []
    for e in pl.get("StartCalendarInterval", []):
        t = f"{e['Hour']:02d}:{e['Minute']:02d}"
        parts.append(f"{names[e['Weekday']]} {t}" if "Weekday" in e else f"daily {t}")
    loaded = _launchctl("print", f"{_domain()}/{LABEL}", check=False).returncode == 0
    return (f"Schedule: {', '.join(parts)}  ({'loaded' if loaded else 'NOT loaded'})\n"
            f"Plist:    {path}\nLog:      {log_path()}\n"
            f"Command:  {' '.join(pl['ProgramArguments'])}")
