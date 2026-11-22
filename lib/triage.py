"""The triage file: a markdown checklist Z edits between collect and apply.

Layout:

    # Tab triage 2026-11-20 14:05
    <!-- stg2gl-triage state=pending -->
    <!-- Move lines between sections. ... -->

    ## unsorted
    - [Title](https://...) `device/iphone` `dev`

    ## read
    ## note
    ## bookmark
    ## skip

Editing rules (also written into the file as a comment):
  - Move a line to another section to change where it goes.
  - Delete a line to drop it without recording it. It will come back next
    collect if the tab is still open. Put it under "skip" to stop seeing it.
  - Text after the tags is kept as a comment. For "note" items it is
    written into the inbox next to the link, so you can jot why it matters.

The state marker is how collect knows whether it may overwrite the file:
"pending" means edits have not been applied yet and collect refuses to
clobber them without --force.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from classify import CATEGORIES, UNSORTED
from stg2gl_core import md_escape_link_text, md_escape_url

SECTIONS = (UNSORTED,) + CATEGORIES  # display order in the file

# Read-only section listing what the scheduled run already sent, so Z can
# spot a bad "auto" rule. Its lines are not items (no leading "["), and it
# is cleared on apply because by then Z has seen it.
SENT_SECTION = "sent automatically"
SENT_COMMENT = ("<!-- Already sent by the scheduled run; listed so you can check the automatic rules.\n"
                "     Moving these lines does nothing. Cleared on apply. -->")

STATE_RE = re.compile(r"<!--\s*stg2gl-triage state=(\w+)\s*-->")

# One item line. Link text may contain escaped characters (\[ \] \\), so it
# is matched as a run of either "\<any char>" or any char except "]" and
# "\". The URL cannot contain ")" or whitespace because md_escape_url
# percent-encodes parentheses, and real URLs have no raw spaces.
ITEM_RE = re.compile(
    r"""^\s*[-*+]\s+                    # list bullet
        (?:\[[ xX]\]\s+)?               # optional task checkbox, ignored
        \[((?:\\.|[^\]\\])*)\]          # [link text]
        \(([^)\s]+)\)                   # (url)
        (.*)$                           # tags and free-text comment
    """,
    re.X,
)
TAG_RE = re.compile(r"`([^`]+)`")
UNESCAPE_RE = re.compile(r"\\(.)")

HELP_COMMENT = (
    "<!-- Move lines between sections to change where they go.\n"
    "     read = GoodLinks, note = Tab inbox, bookmark = Safari import file + Bookmarks inbox,\n"
    "     skip = never show again, unsorted = decide later (kept for next run).\n"
    "     Text after the tags is kept as a comment; on notes it goes into the inbox.\n"
    "     Then run: bin/stg2gl-triage apply -->"
)


@dataclass
class Item:
    title: str
    url: str
    category: str = UNSORTED
    tags: list = field(default_factory=list)
    comment: str = ""

    def to_line(self) -> str:
        tag_str = " ".join(f"`{t}`" for t in self.tags)
        line = f"- [{md_escape_link_text(self.title or self.url)}]({md_escape_url(self.url)})"
        if tag_str:
            line += " " + tag_str
        if self.comment:
            line += " " + self.comment
        return line


def sent_line(it, why: str) -> str:
    """One line for the "sent automatically" section."""
    return f"- {it.category}: [{md_escape_link_text(it.title or it.url)}]({md_escape_url(it.url)}) ({why})"


def render(items, generated_at: str, state: str = "pending", sent=()) -> str:
    """Render items into the triage file text, grouped by category.

    sent: ready-made lines (see sent_line) for the "sent automatically"
    section, which is omitted when empty.
    """
    by_cat = {s: [] for s in SECTIONS}
    for it in items:
        by_cat.get(it.category, by_cat[UNSORTED]).append(it)
    parts = [f"# Tab triage {generated_at}", f"<!-- stg2gl-triage state={state} -->", HELP_COMMENT, ""]
    for s in SECTIONS:
        parts.append(f"## {s}")
        parts.extend(it.to_line() for it in by_cat[s])
        parts.append("")
    if sent:
        parts += [f"## {SENT_SECTION}", SENT_COMMENT, *sent, ""]
    return "\n".join(parts)


def parse_line(line: str, category: str):
    """Parse one item line, or return None if it is not an item."""
    m = ITEM_RE.match(line)
    if not m:
        return None
    title = UNESCAPE_RE.sub(r"\1", m.group(1))
    rest = m.group(3)
    tags = TAG_RE.findall(rest)
    comment = TAG_RE.sub("", rest).strip()
    return Item(title=title, url=m.group(2), category=category, tags=tags, comment=comment)


def parse(text: str):
    """Parse a triage file. Returns (state, items, warnings); see parse_full."""
    state, items, warnings, _sent = parse_full(text)
    return state, items, warnings


def parse_full(text: str):
    """Parse a triage file.

    Returns (state, items, warnings, sent). Items before the first known
    heading, or under a heading that is not a category, are reported in
    warnings and dropped rather than guessed at. `sent` holds the raw
    lines of the "sent automatically" section, kept so a later collect
    can carry them forward until apply clears them.
    """
    state = None
    items, warnings, sent = [], [], []
    current = None  # None = no section yet; "" = unknown section
    for n, line in enumerate(text.splitlines(), 1):
        if state is None:
            m = STATE_RE.search(line)
            if m:
                state = m.group(1)
                continue
        if line.startswith("## "):
            name = line[3:].strip().lower()
            if name == SENT_SECTION:
                current = SENT_SECTION
                continue
            current = name if name in SECTIONS else ""
            if current == "":
                warnings.append(f"line {n}: unknown section {line[3:].strip()!r}; its items are ignored")
            continue
        if current == SENT_SECTION:
            if line.startswith("- "):
                sent.append(line)
            continue
        it = parse_line(line, current or UNSORTED)
        if it is None:
            continue
        if not current:
            warnings.append(f"line {n}: item outside a known section, ignored: {it.url}")
            continue
        items.append(it)
    return state, items, warnings, sent
