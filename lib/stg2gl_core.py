"""Shared logic for stg2gl and stg2gl-triage.

Everything here is pure Python standard library so it runs on the
python3 that ships with macOS (3.9 via the Command Line Tools) and on
Linux for the unit tests. Nothing in this module talks to Safari; the
only side effect is `save_goodlinks`, which shells out to `open`.

These functions used to live inside the Python heredoc in bin/stg2gl.
They were moved here, unchanged in behavior, so the triage command can
reuse the same skip and tag rules instead of keeping a second copy.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.parse


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def norm_tag(t: str) -> str:
    """Lowercase a tag and turn spaces into dashes ("Deep Work" -> "deep-work")."""
    return t.strip().lower().replace(" ", "-")


def host_of(url: str) -> str:
    """Return the lowercased hostname of a URL, or "" if it cannot be parsed."""
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def url_key(url: str) -> str:
    """Identity of a URL for dedupe and the triage ledger.

    - The #fragment is dropped: page.html#intro and page.html#usage are
      the same page.
    - Parentheses are percent-encoded the same way md_escape_url does it,
      so a URL read back from the triage file matches the URL collected
      from Safari.
    Scheme, host, path and query are kept as-is; anything more aggressive
    (trailing slashes, utm_ params) risks merging different pages.
    """
    base = url.split("#", 1)[0]
    return base.replace("(", "%28").replace(")", "%29")


def dedupe_keep_order(items):
    """Drop empty and repeated values while keeping first-seen order."""
    out = []
    seen = set()
    for t in items:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Markdown escaping (shared by stg2gl --output markdown and the triage file)
# ---------------------------------------------------------------------------

def md_escape_link_text(s: str) -> str:
    """Escape characters that break markdown link text: \\ [ ] and newlines."""
    return s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def md_escape_url(s: str) -> str:
    """Escape characters that break markdown link URLs: ( ) and newlines.

    Percent-encoding parentheses keeps the URL valid (servers decode
    %28/%29 back to parentheses) while letting the markdown parser find
    the closing ")" of the link.
    """
    return s.replace("(", "%28").replace(")", "%29").replace("\n", "")


# ---------------------------------------------------------------------------
# Skip + tag rules (config/rules.json)
# ---------------------------------------------------------------------------

class TagRules:
    """Compiled form of the skip and tag sections of rules.json.

    Compiling once up front means a bad regex is reported a single time
    (as a warning, matching the old heredoc behavior) instead of on
    every tab.
    """

    def __init__(self, cfg: dict, warn=None):
        # `warn` lets tests capture warnings; default prints to stderr
        # exactly like the original heredoc did.
        self._warn = warn or (lambda msg: print(msg, file=sys.stderr))

        self.skip_prefixes = tuple(cfg.get("skip_url_prefixes", []))

        # Validate and compile skip regexes (reject invalid patterns early)
        self.skip_regex = []
        for p in cfg.get("skip_url_regex", []):
            try:
                self.skip_regex.append(re.compile(p))
            except re.error as e:
                self._warn(f"Warning: invalid skip_url_regex pattern {p!r}: {e}")

        self.domain_tags = cfg.get("domain_tags", {})

        # Use simple string replacement instead of str.format() to prevent
        # format string injection (e.g. {group.__class__} in a malicious rules.json)
        self.group_tag_template = cfg.get("group_tag_format", "tg/{group}")

        # Pre-compile and validate keyword_tags regexes
        self.keyword_tags = []
        for rule in cfg.get("keyword_tags", []):
            if "tags" not in rule:
                self._warn(f"Warning: keyword_tags rule missing 'tags' key, skipping: {rule!r}")
                continue
            try:
                pat = re.compile(rule["pattern"], re.I)
                self.keyword_tags.append((pat, rule))
            except (re.error, KeyError) as e:
                self._warn(f"Warning: invalid keyword_tags rule {rule.get('pattern', '?')!r}: {e}")

    # -- skipping ----------------------------------------------------------

    def should_skip(self, url: str):
        """Check if a URL should be skipped.

        Returns False if the URL is fine, or a string describing why it is
        skipped. The strings are parsed by stg2gl's verification report
        ("unsaveable ..." and "empty URL" are listed individually), so do
        not reword them.
        """
        if not url:
            return "empty URL"
        for p in self.skip_prefixes:
            if url.startswith(p):
                return f"unsaveable URL (prefix: {p})"
        for rx in self.skip_regex:
            if rx.search(url):
                return f"blocked pattern ({rx.pattern})"
        return False

    # -- tagging -----------------------------------------------------------

    def group_tag(self, group_name: str) -> str:
        """Render group_tag_format for one group, e.g. "Work" -> "tg/work"."""
        # Safe string replacement — no Python format string interpretation
        return norm_tag(self.group_tag_template.replace("{group}", group_name))

    def content_tags(self, url: str, title: str):
        """Tags derived only from the URL and title (domain + keyword rules).

        The triage command uses this directly because it supplies its own
        source tags (window / tab group / device) instead of one group tag.
        """
        tags = []
        host = host_of(url)
        for k, v in self.domain_tags.items():
            if k.lower() in host:
                tags.extend([norm_tag(x) for x in v])

        title_l = (title or "").lower()
        url_l = url.lower()
        for pat, rule in self.keyword_tags:
            fields = rule.get("fields", ["title", "url"])
            hay = ""
            if "title" in fields:
                hay += title_l + " "
            if "url" in fields:
                hay += url_l
            if pat.search(hay):
                tags.extend([norm_tag(x) for x in rule["tags"]])
        return dedupe_keep_order(tags)

    def apply_rules(self, url: str, title: str, group_name: str, base_tags=()):
        """Full stg2gl tag list, in the documented order:
        group tag, base tags, domain tags, keyword tags. Deduplicated."""
        tags = [self.group_tag(group_name)]
        for bt in base_tags:
            if bt:
                tags.append(norm_tag(bt))
        tags.extend(self.content_tags(url, title))
        return dedupe_keep_order(tags)


# ---------------------------------------------------------------------------
# GoodLinks output
# ---------------------------------------------------------------------------

def check_goodlinks_available():
    """Pre-flight: verify GoodLinks.app is installed and can handle its URL scheme.

    Exits the process (status 1) if it is missing, so a long batch never
    starts and then fails on every item.
    """
    result = subprocess.run(["open", "-Ra", "GoodLinks"], capture_output=True)
    if result.returncode != 0:
        print(
            "Error: GoodLinks.app not found. Install it or use --output json/sqlite/markdown.",
            file=sys.stderr,
        )
        sys.exit(1)


def goodlinks_save_url(url: str, tags) -> str:
    """Build the goodlinks:// x-callback URL for one save (no side effects)."""
    return "goodlinks://x-callback-url/save?quick=1&url={u}&tags={t}".format(
        u=urllib.parse.quote(url, safe=""),
        t=urllib.parse.quote(" ".join(tags), safe=""),
    )


def save_goodlinks(url: str, tags, throttle_ms: int):
    """Save via GoodLinks URL scheme (triggers iCloud sync).

    Returns True on success, or an error string on failure. Sleeps
    `throttle_ms` after each call because GoodLinks drops requests that
    arrive too quickly.
    """
    result = subprocess.run(["open", goodlinks_save_url(url, tags)], capture_output=True)
    time.sleep(throttle_ms / 1000.0)
    if result.returncode != 0:
        stderr_msg = result.stderr.decode("utf-8", errors="replace").strip()
        return f"open exited {result.returncode}: {stderr_msg}" if stderr_msg else f"open exited {result.returncode}"
    return True
