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

import stg2gl_core as core
from stg2gl_core import site_of, url_key

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
    """SQLite state for stg2gl-triage (default ~/.stg2gl/ledger.db).

    Tables:
      routed     every URL already routed, keyed by url_key(). collect
                 hides these, so a tab that stays open for weeks is only
                 triaged once. INSERT OR REPLACE lets a later run (with
                 --include-seen) change the category.
      guesses    the tool's guess for each tab waiting in the triage file,
                 so apply can tell what Z changed. Deleted once routed.
      decisions  one row per tab Z routed: site, guess, final category.
                 learn.py derives learned rules and statistics from it.
      runs       one row per collect: how many tabs were classified and
                 how many no rule could place. Feeds `status` advice.
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
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guesses (
                url_key TEXT PRIMARY KEY,
                site    TEXT NOT NULL,
                guess   TEXT NOT NULL,
                why     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                url_key  TEXT NOT NULL,
                site     TEXT NOT NULL,
                guessed  TEXT NOT NULL,
                why      TEXT NOT NULL,
                final    TEXT NOT NULL,
                at       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS decisions_site ON decisions(site);
            CREATE TABLE IF NOT EXISTS runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                at          TEXT NOT NULL,
                kind        TEXT NOT NULL,
                total       INTEGER NOT NULL,
                unsorted    INTEGER NOT NULL,
                auto_routed INTEGER NOT NULL,
                labeled     INTEGER NOT NULL
            );
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

    # -- guesses / decisions (learning) ------------------------------------

    def remember_guess(self, url: str, guess: str, why: str):
        """Store the guess for a tab entering the triage file. Keeps an
        existing guess, because a tab carried over from an earlier collect
        was first judged then."""
        self.conn.execute(
            "INSERT OR IGNORE INTO guesses (url_key, site, guess, why) VALUES (?, ?, ?, ?)",
            (url_key(url), site_of(url), guess, why),
        )

    def record_decision(self, url: str, final: str):
        """Called when apply routes a tab: compare Z's choice to the guess."""
        key = url_key(url)
        row = self.conn.execute("SELECT guess, why FROM guesses WHERE url_key = ?", (key,)).fetchone()
        guessed, why = row if row else ("unsorted", "no stored guess")
        self.conn.execute(
            "INSERT INTO decisions (url_key, site, guessed, why, final, at) VALUES (?, ?, ?, ?, ?, ?)",
            (key, site_of(url), guessed, why, final, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.execute("DELETE FROM guesses WHERE url_key = ?", (key,))

    def prune_guesses(self, keep_keys):
        """Drop guesses for tabs no longer in the triage file (Z deleted the
        line). Without this the table would only ever grow."""
        keep = set(keep_keys)
        stale = [k for (k,) in self.conn.execute("SELECT url_key FROM guesses") if k not in keep]
        self.conn.executemany("DELETE FROM guesses WHERE url_key = ?", [(k,) for k in stale])

    def forget_site(self, site: str) -> int:
        cur = self.conn.execute("DELETE FROM decisions WHERE site = ?", (site,))
        return cur.rowcount

    def record_run(self, kind: str, total: int, unsorted: int, auto_routed: int, labeled: int):
        self.conn.execute(
            "INSERT INTO runs (at, kind, total, unsorted, auto_routed, labeled) VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), kind, total, unsorted, auto_routed, labeled),
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


# ---------------------------------------------------------------------------
# Routing a batch (shared by `apply` and the scheduled `auto` run)
# ---------------------------------------------------------------------------

# Tabs routed to these categories are closed afterwards when close_tabs is
# on. "skip" is deliberately absent: skipped tabs are things like webmail
# and calendars, which you are probably still using.
CLOSE_AFTER = ("read", "note", "bookmark")


def route_batch(groups, tcfg, throttle_ms: int, ledger, now, learn: bool):
    """Send each category to its destination.

    groups: {category: [Item]} for read/note/bookmark/skip.
    learn:  True for apply (Z made these choices, record decisions);
            False for auto (the tool made them, nothing to learn).

    Never raises for a destination failure: failed items are returned so
    the caller can keep them in the triage file for a retry. Returns
    {"routed": {cat: n}, "routed_items": [Item], "failed": [(Item, why)],
     "written": [path]}.
    """
    date, stamp = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d-%H%M")
    vault = os.path.expanduser(tcfg["vault_path"])
    note_path = os.path.join(vault, tcfg["note_inbox"])
    bm_path = os.path.join(vault, tcfg["bookmark_inbox"])
    res = {"routed": {c: 0 for c in ("read", "note", "bookmark", "skip")},
           "routed_items": [], "failed": [], "written": []}

    def done(items, cat):
        for it in items:
            ledger.record(it.url, it.title, cat)
            if learn:
                ledger.record_decision(it.url, cat)
        res["routed"][cat] += len(items)
        res["routed_items"].extend(items)
        ledger.commit()  # commit per category: a crash later keeps this work

    # read -> GoodLinks, one at a time; a failure affects only that item.
    reads = groups.get("read", [])
    if reads:
        if not core.goodlinks_available():
            res["failed"].extend((it, "GoodLinks.app not found") for it in reads)
        else:
            ok = []
            for it in reads:
                r = core.save_goodlinks(it.url, it.tags, throttle_ms)
                if r is True:
                    ok.append(it)
                else:
                    res["failed"].append((it, r))
            done(ok, "read")

    # note -> one append; all or nothing.
    notes = groups.get("note", [])
    if notes:
        try:
            append_note_inbox(note_path, notes, date)
            res["written"].append(note_path)
            done(notes, "note")
        except OSError as e:
            res["failed"].extend((it, f"note inbox: {e}") for it in notes)

    # bookmark -> import file + vault inbox; routed only if both succeed.
    bms = groups.get("bookmark", [])
    if bms:
        try:
            html_path = write_bookmarks_html(tcfg["bookmarks_html_dir"], bms, stamp)
            res["written"].append(html_path)
            append_bookmark_inbox(bm_path, bms, date)
            res["written"].append(bm_path)
            done(bms, "bookmark")
        except OSError as e:
            res["failed"].extend((it, f"bookmarks: {e}") for it in bms)

    # skip -> ledger only.
    done(groups.get("skip", []), "skip")
    return res


def tabs_to_close(routed_items, tcfg):
    """url_keys of routed tabs that should be closed, honouring config."""
    if not tcfg.get("close_tabs", True):
        return []
    keep = [d.lower() for d in tcfg.get("close_tabs_keep_domains", [])]
    out = []
    for it in routed_items:
        if it.category not in CLOSE_AFTER:
            continue
        host = core.host_of(it.url)
        if any(k in host for k in keep):
            continue
        out.append(url_key(it.url))
    return out
