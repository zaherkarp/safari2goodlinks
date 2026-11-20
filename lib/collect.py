"""Tab collectors for stg2gl-triage.

Each collector returns a list of records:

    {"title": str, "url": str, "source": str}

where `source` says where the tab was open:

    "window"           an open Safari window on this Mac
    "tg/<group>"       a Safari Tab Group (rendered with group_tag_format)
    "device/<name>"    a tab open on another device, via iCloud Tabs

A collector that cannot run (no Safari, no permission, unknown database
schema) raises CollectError. The caller prints it as a warning and keeps
going with the other sources, so one broken source never blocks a run.

UNVERIFIED: the two SQLite readers are written against the schemas I
expect Safari to use (CloudTabs.db: tables cloud_tabs + cloud_tab_devices;
SafariTabs.db: a `bookmarks` tree table). Apple does not document either
file. Both readers check for the tables and columns they need and raise
CollectError with the actual schema summary if something is missing, so a
schema change shows up as a clear warning instead of wrong data.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager

from stg2gl_core import norm_tag


class CollectError(Exception):
    """A source could not be read. The message is shown to the user."""


# ---------------------------------------------------------------------------
# osascript helper
# ---------------------------------------------------------------------------

def run_osascript(script_path: str, args=(), timeout: int = 60) -> str:
    """Run an AppleScript file and return its stdout.

    subprocess's own timeout replaces the sleep/kill watcher used in
    bin/stg2gl (macOS has no `timeout` command, but Python has one).
    """
    if not shutil.which("osascript"):
        raise CollectError("osascript not found (this source only works on macOS)")
    try:
        res = subprocess.run(
            ["osascript", script_path, *args],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CollectError(f"{os.path.basename(script_path)} timed out after {timeout}s")
    if res.returncode != 0:
        msg = res.stderr.decode("utf-8", errors="replace").strip()
        raise CollectError(f"{os.path.basename(script_path)} failed: {msg or 'exit ' + str(res.returncode)}")
    return res.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Source 1: every open Safari window
# ---------------------------------------------------------------------------

def parse_window_lines(text: str):
    """Parse export_all_windows.applescript output (3 lines per tab).

    Split on "\\n" only. str.splitlines() would also split on Unicode line
    separators (U+2028 etc.), which AppleScript does not strip and which a
    page title can contain, and that would break the 3-line grouping.
    """
    text = text.rstrip("\n")
    if not text:
        return []
    lines = text.split("\n")
    if len(lines) % 3 != 0:
        # Should not happen given the CR/LF stripping in the script. Refuse
        # rather than guess, because a shifted grouping pairs titles with
        # the wrong URLs.
        raise CollectError(f"window export returned {len(lines)} lines, not a multiple of 3")
    out = []
    for i in range(0, len(lines), 3):
        out.append({"title": lines[i + 1], "url": lines[i + 2], "source": "window"})
    return out


def from_windows(repo_dir: str):
    """All tabs in all open Safari windows on this Mac."""
    script = os.path.join(repo_dir, "scripts", "export_all_windows.applescript")
    return parse_window_lines(run_osascript(script))


# ---------------------------------------------------------------------------
# SQLite helpers (shared by the Tab Group and iCloud Tabs readers)
# ---------------------------------------------------------------------------

@contextmanager
def open_sqlite_copy(db_path: str):
    """Open a private copy of a Safari database, read-only in spirit.

    Safari keeps these files open in WAL mode. Copying the main file plus
    its -wal and -shm siblings into a temp dir gives a consistent snapshot
    without contending for Safari's locks and without any chance of us
    writing to Safari's real file.

    Reading ~/Library/Containers/com.apple.Safari needs Full Disk Access
    for the app running the script (Terminal, iTerm, ...). Without it the
    copy fails with PermissionError, reported as a CollectError.
    """
    db_path = os.path.expanduser(db_path)
    if not os.path.exists(db_path):
        raise CollectError(f"not found: {db_path}")
    tmp = tempfile.mkdtemp(prefix="stg2gl-")
    try:
        dst = os.path.join(tmp, os.path.basename(db_path))
        try:
            shutil.copy2(db_path, dst)
            for suffix in ("-wal", "-shm"):
                if os.path.exists(db_path + suffix):
                    shutil.copy2(db_path + suffix, dst + suffix)
        except PermissionError:
            raise CollectError(
                f"permission denied reading {db_path}. Give your terminal app Full Disk Access "
                "(System Settings > Privacy & Security > Full Disk Access)."
            )
        conn = sqlite3.connect(dst)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    finally:
        # The copy contains browsing data; do not leave it in /tmp.
        shutil.rmtree(tmp, ignore_errors=True)


def table_columns(conn, table: str):
    """Set of column names for `table`, or empty set if it does not exist."""
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table!r})")}


def schema_summary(conn) -> str:
    """One line per table with its columns, for error messages."""
    names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return "; ".join(f"{n}({', '.join(sorted(table_columns(conn, n)))})" for n in names)


def require_columns(conn, table: str, needed: set, what: str):
    cols = table_columns(conn, table)
    missing = needed - cols
    if missing:
        raise CollectError(
            f"{what}: table {table!r} is missing columns {sorted(missing)}. "
            f"Safari's schema may have changed. Found: {schema_summary(conn)}"
        )
    return cols


# ---------------------------------------------------------------------------
# Source 2: Tab Groups
# ---------------------------------------------------------------------------

def from_tab_groups_db(db_path: str, group_tag_format: str, exclude_groups=()):
    """Every tab in every named Tab Group, read from SafariTabs.db.

    Expected layout (UNVERIFIED, see module docstring): one `bookmarks`
    table holding a tree. A Tab Group is a row with a title and no URL;
    its tabs are child rows (parent = group id) that have a URL. Rows
    flagged hidden or deleted are ignored when those columns exist.

    Groups with an empty title are skipped: those are the unnamed
    per-window groups, which the window collector already covers.
    """
    excluded = {g.lower() for g in exclude_groups}
    with open_sqlite_copy(db_path) as conn:
        cols = require_columns(conn, "bookmarks", {"id", "parent", "title", "url"}, "SafariTabs.db")

        # Build optional filters only from columns that actually exist, so
        # the query still runs on older or newer schema variants.
        live = []
        for c in ("hidden", "deleted"):
            if c in cols:
                live.append(f"COALESCE(t.{c}, 0) = 0")
                live.append(f"COALESCE(g.{c}, 0) = 0")
        where = " AND ".join(["COALESCE(t.url, '') <> ''", "COALESCE(g.title, '') <> ''", *live])

        order = "g.title, t.order_index" if "order_index" in cols else "g.title, t.id"
        rows = conn.execute(
            f"""
            SELECT g.title AS grp, t.title AS title, t.url AS url
            FROM bookmarks t
            JOIN bookmarks g ON g.id = t.parent
            WHERE {where}
            ORDER BY {order}
            """
        ).fetchall()

    out = []
    for r in rows:
        if r["grp"].lower() in excluded:
            continue
        out.append({
            "title": r["title"] or "",
            "url": r["url"],
            # Same rendering as stg2gl's group tag: "Work" -> "tg/work"
            "source": norm_tag(group_tag_format.replace("{group}", r["grp"])),
        })
    return out


def from_tab_groups_gui(repo_dir: str, names, group_tag_format: str):
    """Fallback: click each named group in the sidebar, then read the front window.

    This is the same GUI scripting bin/stg2gl --mode select uses. It needs
    Accessibility permission, it switches the front window to each group
    in turn, and partial name matching applies ("work" can hit "Homework").
    """
    select = os.path.join(repo_dir, "scripts", "select_tab_group.applescript")
    export = os.path.join(repo_dir, "scripts", "export_all_windows.applescript")
    out = []
    for name in names:
        run_osascript(select, [name], timeout=30)
        tag = norm_tag(group_tag_format.replace("{group}", name))
        # After selection the group is in the front window, which AppleScript
        # reports as window 1. Keep only window 1's tabs.
        text = run_osascript(export)
        lines = text.rstrip("\n").split("\n") if text.strip() else []
        for i in range(0, len(lines) - 2, 3):
            if lines[i] == "1":
                out.append({"title": lines[i + 1], "url": lines[i + 2], "source": tag})
    return out


# ---------------------------------------------------------------------------
# Source 3: iCloud Tabs (iPhone / iPad / other Macs)
# ---------------------------------------------------------------------------

def local_computer_name():
    """This Mac's sharing name (what iCloud Tabs shows), or None off macOS."""
    if not shutil.which("scutil"):
        return None
    try:
        res = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        return None
    name = res.stdout.decode("utf-8", errors="replace").strip()
    return name or None


def from_icloud(db_path: str, exclude_devices=()):
    """Tabs open on your other devices, read from CloudTabs.db.

    Expected layout (UNVERIFIED): cloud_tabs(title, url, device_uuid) and
    cloud_tab_devices(device_uuid, device_name). This Mac's own entry, if
    present, is dropped because its tabs come from the window collector.
    """
    excluded = {d.lower() for d in exclude_devices}
    me = local_computer_name()
    if me:
        excluded.add(me.lower())

    with open_sqlite_copy(db_path) as conn:
        require_columns(conn, "cloud_tabs", {"title", "url", "device_uuid"}, "CloudTabs.db")
        require_columns(conn, "cloud_tab_devices", {"device_uuid", "device_name"}, "CloudTabs.db")
        order = "d.device_name, t.position" if "position" in table_columns(conn, "cloud_tabs") else "d.device_name"
        rows = conn.execute(
            f"""
            SELECT d.device_name AS device, t.title AS title, t.url AS url
            FROM cloud_tabs t
            JOIN cloud_tab_devices d ON d.device_uuid = t.device_uuid
            ORDER BY {order}
            """
        ).fetchall()

    out = []
    for r in rows:
        device = r["device"] or "unknown"
        if device.lower() in excluded:
            continue
        out.append({
            "title": r["title"] or "",
            "url": r["url"] or "",
            "source": "device/" + norm_tag(device),
        })
    return out
