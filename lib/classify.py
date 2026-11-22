"""Guess a triage category for each tab: read, note, bookmark, or skip.

The guess is a starting point. Tabs land in the triage file under their
guessed section and Z moves lines he disagrees with. Anything no rule
matches goes to "unsorted", so a wrong guess is never silent.

Rules come from "category_rules" in the "triage" section of rules.json:

    {
      "category": "read" | "note" | "bookmark" | "skip" | "unsorted",
      "domains":     ["substack.com", ...],   # optional, hostname substring
      "url_regex":   "...",                   # optional, case-insensitive
      "title_regex": "...",                   # optional, case-insensitive
      "page":        ["recipe", ...],         # optional, page labels (hints.py)
      "auto":        true,                    # optional, see below
      "why":         "free text"              # optional, shown in --dry-run
    }

A rule matches when ALL the conditions it lists match. A rule with a
"page" condition can only match when page labels were fetched for that
tab (triage.page_hints), so labels stay soft unless Z writes such a rule.

"auto": true means `stg2gl-triage auto` (the scheduled run) sends tabs
matched by this rule straight to their destination without waiting for
review. Only hand-written rules can be auto; learned rules never are.

Evaluation order:
  1. "skip" rules, in file order. A login page on a bare domain should be
     skipped, not bookmarked.
  2. Place-or-thing: a site root (https://example.com/) is a bookmark.
     Turn off with "bookmark_bare_domains": false.
  3. All other hand-written rules, in file order. First match wins.
  4. Learned rules (learn.py): sites Z has sorted the same way several
     times. They come after hand-written rules so config always wins.
  5. Otherwise "unsorted".
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from dataclasses import dataclass

from stg2gl_core import host_of, site_of

CATEGORIES = ("read", "note", "bookmark", "skip")
UNSORTED = "unsorted"
PAGE_PREFIX = "page:"  # tag prefix for page labels, e.g. "page:recipe"


@dataclass
class Guess:
    category: str
    why: str
    auto: bool = False  # True only for hand-written rules marked "auto"


class Classifier:
    def __init__(self, triage_cfg: dict, learned=(), warn=None):
        """learned: iterable of (site, category, why) from learn.learned_rules()."""
        self._warn = warn or (lambda msg: print(msg, file=sys.stderr))
        self.bare_domain_is_bookmark = triage_cfg.get("bookmark_bare_domains", True)
        self.learned = list(learned)

        # Compile once; a broken rule is reported and dropped, the rest run.
        self.skip_rules = []
        self.other_rules = []
        for i, raw in enumerate(triage_cfg.get("category_rules", [])):
            rule = self._compile(i, raw)
            if rule is None:
                continue
            (self.skip_rules if rule["category"] == "skip" else self.other_rules).append(rule)

    def _compile(self, i: int, raw: dict):
        cat = raw.get("category")
        # "unsorted" is allowed in rules so a broad rule can have exceptions:
        # put {"domains": ["docs.google.com"], "category": "unsorted"} above
        # {"domains": ["docs."], "category": "bookmark"} to force review.
        if cat not in CATEGORIES + (UNSORTED,):
            self._warn(f"Warning: category_rules[{i}] has category {cat!r}; expected one of {CATEGORIES + (UNSORTED,)}. Skipping rule.")
            return None
        rule = {
            "category": cat,
            "domains": [d.lower() for d in raw.get("domains", [])],
            "page": {p.lower() for p in raw.get("page", [])},
            "url_rx": None,
            "title_rx": None,
            # Auto-sending "unsorted" makes no sense; ignore the flag there.
            "auto": bool(raw.get("auto")) and cat != UNSORTED,
            "why": raw.get("why") or f"category_rules[{i}]",
        }
        try:
            if raw.get("url_regex"):
                rule["url_rx"] = re.compile(raw["url_regex"], re.I)
            if raw.get("title_regex"):
                rule["title_rx"] = re.compile(raw["title_regex"], re.I)
        except re.error as e:
            self._warn(f"Warning: category_rules[{i}] has an invalid regex: {e}. Skipping rule.")
            return None
        if not (rule["domains"] or rule["url_rx"] or rule["title_rx"] or rule["page"]):
            # A rule with no conditions would match everything and silently
            # swallow the "unsorted" safety net.
            self._warn(f"Warning: category_rules[{i}] has no domains/url_regex/title_regex/page. Skipping rule.")
            return None
        return rule

    @staticmethod
    def _matches(rule, url: str, host: str, title: str, pages: set) -> bool:
        # AND across the conditions a rule lists; absent conditions are ignored.
        if rule["domains"] and not any(d in host for d in rule["domains"]):
            return False
        if rule["url_rx"] and not rule["url_rx"].search(url):
            return False
        if rule["title_rx"] and not rule["title_rx"].search(title or ""):
            return False
        if rule["page"] and not (rule["page"] & pages):
            return False
        return True

    @staticmethod
    def is_bare_domain(url: str) -> bool:
        """True for a site root: no path beyond "/", no query.

        A fragment alone (https://x.com/#top) still counts as the root.
        """
        try:
            p = urllib.parse.urlparse(url)
        except Exception:
            return False
        return p.scheme in ("http", "https") and bool(p.netloc) and p.path in ("", "/") and not p.query

    def classify(self, url: str, title: str, tags=()) -> Guess:
        """Guess a category. `tags` may include page labels ("page:recipe")."""
        host = host_of(url)
        pages = {t[len(PAGE_PREFIX):] for t in tags if t.startswith(PAGE_PREFIX)}
        for r in self.skip_rules:
            if self._matches(r, url, host, title, pages):
                return Guess("skip", r["why"], r["auto"])
        if self.bare_domain_is_bookmark and self.is_bare_domain(url):
            return Guess("bookmark", "site root (place, not thing)")
        for r in self.other_rules:
            if self._matches(r, url, host, title, pages):
                return Guess(r["category"], r["why"], r["auto"])
        site = site_of(url)
        for lsite, cat, why in self.learned:
            # Exact site or a subdomain of it.
            if site == lsite or site.endswith("." + lsite):
                return Guess(cat, why)
        return Guess(UNSORTED, "no rule matched")
