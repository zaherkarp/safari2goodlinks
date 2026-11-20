"""Destinations for triaged tabs, plus the ledger of what was already routed.

    read      -> GoodLinks (stg2gl_core.save_goodlinks)
    note      -> <vault>/Tab inbox.md            (append_note_inbox)
    bookmark  -> Netscape HTML for Safari import (write_bookmarks_html)
                 + <vault>/bookmarks/Bookmarks inbox.md (append_bookmark_inbox)
    skip      -> ledger only, so the tab stops reappearing

Writers only ever append to vault notes. They never rewrite or reorder
what is already there, because Z edits those notes by hand.
"""

from __future__ import annotations

import html
import os
import sqlite3
from datetime import datetime, timezone

from stg2gl_core import url_key

# Tags that say where a tab came from rather than what it is about. They are
# kept on GoodLinks saves but ignored when picking a topic heading.
SOURCE_TAG_PREFIXES = ("tg/", "device/")
SOURCE_TAGS = {"window"}


def topic_tags(tags):
    return [t for t in tags if t not in SOURCE_TAGS and not t.startswith(SOURCE_TAG_PREFIXES)]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class Ledger:
    """SQLite record of every URL already routed, keyed by url_key().

    collect hides ledger URLs so a tab that stays open for weeks is only
    triaged once. Recording uses INSERT OR REPLACE so moving a URL to a
    different category on a later run (with --include-seen) updates it.
    """

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS routed (
                url_key   TEXT PRIMARY KEY,
                url       TEXT NOT NULL,
                title     TEXT,
                category  TEXT NOT NULL,
                routed_at TEXT NOT NULL
            )
            """
        )

    def seen(self, url: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM routed WHERE url_key = ?", (url_key(url),))
        return cur.fetchone() is not None

    def record(self, url: str, title: str, category: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO routed (url_key, url, title, category, routed_at) VALUES (?, ?, ?, ?, ?)",
            (url_key(url), url, title, category, datetime.now(timezone.utc).isoformat()),
        )

    def counts(self):
        return dict(self.conn.execute("SELECT category, COUNT(*) FROM routed GROUP BY category").fetchall())

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()


# ---------------------------------------------------------------------------
# Vault notes
# ---------------------------------------------------------------------------

NOTE_INBOX_HEADER = (
    "# Tab inbox\n\n"
    "Tabs triaged as informative notes by stg2gl-triage. "
    "Turn each into a note or fold it into an existing one, then delete the line.\n"
)

BOOKMARK_INBOX_HEADER = (
    "# Bookmarks inbox\n\n"
    "Tabs triaged as bookmarks (places, not things) by stg2gl-triage. "
    "The same links are in the Safari import file. "
    "Move each into its list note (Gear Shops, Companies of Interest, ...) or delete it.\n"
)


def _append(path: str, header: str, body: str):
    """Append body to a note, creating it with header if it does not exist.

    Refuses to create missing parent folders outside the vault root: if
    the folder is missing, the vault path is probably wrong, and writing a
    stray tree somewhere is worse than failing.
    """
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        raise FileNotFoundError(f"folder does not exist: {parent} (check triage.vault_path)")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(header)
        f.write(body)


def append_note_inbox(path: str, items, date: str):
    """Add a dated section of note items. Comments ride along after the link."""
    lines = [it.to_line() for it in items]
    _append(path, NOTE_INBOX_HEADER, f"\n## {date}\n\n" + "\n".join(lines) + "\n")


def append_bookmark_inbox(path: str, items, date: str):
    """Add a dated section, grouped under H3 headings by first topic tag.

    Items with no topic tag go under "other". Group order follows first
    appearance so related tabs stay together as collected.
    """
    groups = {}
    for it in items:
        topics = topic_tags(it.tags)
        groups.setdefault(topics[0] if topics else "other", []).append(it)
    parts = [f"\n## {date}\n"]
    for topic, its in groups.items():
        parts.append(f"\n### {topic}\n")
        # Bookmark lists in the vault (gear/gear-shops.md) use bare links,
        # so drop the tags here; the topic heading carries that information.
        for it in its:
            bare = type(it)(title=it.title, url=it.url, category=it.category, tags=[], comment=it.comment)
            parts.append(bare.to_line() + "\n")
    _append(path, BOOKMARK_INBOX_HEADER, "".join(parts))


# ---------------------------------------------------------------------------
# Safari import file
# ---------------------------------------------------------------------------

def bookmarks_html(items, folder: str) -> str:
    """Netscape bookmark file, the format Safari's File > Import From >
    Bookmarks HTML File reads. All links go into one folder named `folder`,
    which Safari places inside its "Imported" folder.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    rows = []
    for it in items:
        rows.append(
            f'        <DT><A HREF="{html.escape(it.url, quote=True)}" ADD_DATE="{now}">'
            f"{html.escape(it.title or it.url)}</A>"
        )
    return (
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>\n"
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">\n'
        "<TITLE>Bookmarks</TITLE>\n"
        "<H1>Bookmarks</H1>\n"
        "<DL><p>\n"
        f"    <DT><H3 ADD_DATE=\"{now}\">{html.escape(folder)}</H3>\n"
        "    <DL><p>\n"
        + "\n".join(rows)
        + "\n    </DL><p>\n"
        "</DL><p>\n"
    )


def write_bookmarks_html(out_dir: str, items, stamp: str) -> str:
    """Write the import file and return its path. Never overwrites: the
    filename carries date and time, with a counter if that still collides."""
    out_dir = os.path.expanduser(out_dir)
    if not os.path.isdir(out_dir):
        raise FileNotFoundError(f"folder does not exist: {out_dir} (check triage.bookmarks_html_dir)")
    base = os.path.join(out_dir, f"stg2gl-bookmarks-{stamp}")
    path, n = base + ".html", 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}.html"
    with open(path, "x", encoding="utf-8") as f:
        f.write(bookmarks_html(items, f"From tabs {stamp}"))
    return path
