"""Test fixtures shared by the test modules.

Safari, osascript and GoodLinks do not exist on Linux (where these tests
are developed), so the end-to-end tests put fake `osascript` and `open`
executables first on PATH:

  fake osascript  prints canned output chosen by the script name it was
                  given, so bin/stg2gl and bin/stg2gl-triage run unchanged.
  fake open       appends its arguments to a log file and exits 0, standing
                  in for both `open -Ra GoodLinks` and the goodlinks:// save.

The fixture databases follow the schemas lib/collect.py expects. Those
schemas are UNVERIFIED against a real Mac (see lib/collect.py); if Step 0
of the plan shows a different layout, update both the reader and these
fixtures.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))


def write_exe(path: str, body: str):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_fakes(bin_dir: str, window_tabs, front_tabs=None, open_log=None):
    """Create fake osascript/open in bin_dir.

    window_tabs: list of (window_index, title, url) for export_all_windows.
    front_tabs:  list of {"title","url"} for export_tabs (bin/stg2gl).
    """
    os.makedirs(bin_dir, exist_ok=True)
    all_windows = "\n".join(f"{w}\n{t}\n{u}" for w, t, u in window_tabs)
    front_json = json.dumps(front_tabs or [])
    data_dir = os.path.join(bin_dir, "_data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "all_windows.txt"), "w") as f:
        f.write(all_windows)
    with open(os.path.join(data_dir, "front.json"), "w") as f:
        f.write(front_json)

    write_exe(os.path.join(bin_dir, "osascript"), textwrap.dedent(f"""\
        #!/bin/sh
        case "$1" in
          *export_all_windows.applescript) cat "{data_dir}/all_windows.txt"; echo ;;
          *export_tabs.applescript) cat "{data_dir}/front.json"; echo ;;
          *select_tab_group.applescript) exit 0 ;;
          *) echo "fake osascript: unexpected $1" >&2; exit 1 ;;
        esac
        """))
    log = open_log or os.path.join(bin_dir, "open.log")
    write_exe(os.path.join(bin_dir, "open"), textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "{log}"
        exit 0
        """))
    return log


def make_cloudtabs_db(path: str, devices):
    """devices: {device_name: [(title, url), ...]}"""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE cloud_tab_devices (device_uuid TEXT PRIMARY KEY, system_fields BLOB,
            device_name TEXT, has_duplicate_device_name BOOLEAN, is_ephemeral_device BOOLEAN,
            last_modified REAL);
        CREATE TABLE cloud_tabs (tab_uuid TEXT PRIMARY KEY, system_fields BLOB, device_uuid TEXT,
            position BLOB, title TEXT, url TEXT, is_showing_reader BOOLEAN, is_pinned BOOLEAN);
    """)
    for d_i, (name, tabs) in enumerate(devices.items()):
        duid = f"dev-{d_i}"
        conn.execute("INSERT INTO cloud_tab_devices (device_uuid, device_name) VALUES (?, ?)", (duid, name))
        for t_i, (title, url) in enumerate(tabs):
            conn.execute(
                "INSERT INTO cloud_tabs (tab_uuid, device_uuid, position, title, url) VALUES (?, ?, ?, ?, ?)",
                (f"{duid}-{t_i}", duid, t_i, title, url),
            )
    conn.commit()
    conn.close()


def make_safaritabs_db(path: str, groups):
    """groups: {group_title: [(title, url), ...]}. An empty title makes an
    unnamed (per-window) group, which the reader should ignore. One hidden
    tab is added to every group to check the hidden filter."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE bookmarks (id INTEGER PRIMARY KEY, parent INTEGER, type INTEGER,
        title TEXT, url TEXT, hidden INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0, order_index INTEGER)""")
    conn.execute("INSERT INTO bookmarks (id, parent, type, title) VALUES (0, NULL, 1, 'Root')")
    next_id = 1
    for g_title, tabs in groups.items():
        gid = next_id
        next_id += 1
        conn.execute("INSERT INTO bookmarks (id, parent, type, title) VALUES (?, 0, 1, ?)", (gid, g_title))
        for i, (title, url) in enumerate(tabs):
            conn.execute(
                "INSERT INTO bookmarks (id, parent, type, title, url, order_index) VALUES (?, ?, 0, ?, ?, ?)",
                (next_id, gid, title, url, i),
            )
            next_id += 1
        conn.execute(
            "INSERT INTO bookmarks (id, parent, type, title, url, hidden) VALUES (?, ?, 0, 'hidden', 'https://hidden.example/x', 1)",
            (next_id, gid),
        )
        next_id += 1
    conn.commit()
    conn.close()
