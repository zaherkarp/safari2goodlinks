"""Guess a triage category for each tab: read, note, bookmark, or skip.

The guess is only a starting point. Every tab lands in the triage file
under its guessed section and Z moves lines he disagrees with. Anything no
rule matches goes to "unsorted" so a wrong guess is never silent.

Rules come from the "category_rules" list in the "triage" section of
rules.json. Each rule:

    {
      "category": "read" | "note" | "bookmark" | "skip" | "unsorted",
      "domains":     ["substack.com", ...],   # optional, hostname substring
      "url_regex":   "...",                   # optional, case-insensitive
      "title_regex": "...",                   # optional, case-insensitive
      "why":         "free text"              # optional, shown in --explain
    }

A rule matches when ALL the conditions it lists match (a rule that lists
only `domains` matches on domain alone). Evaluation order:

  1. "skip" rules, in file order. A login page on a bare domain should be
     skipped, not bookmarked, so skips win over everything.
  2. The place-or-thing heuristic from the 2026-08 bookmarks cleanup: a
     URL with no path (https://example.com or https://example.com/) is a
     place you go back to, so it is a bookmark. Turn off with
     "bookmark_bare_domains": false.
  3. All other rules, in file order. First match wins.
  4. Otherwise "unsorted".
"""

from __future__ import annotations

import re
import sys
import urllib.parse

from stg2gl_core import host_of

CATEGORIES = ("read", "note", "bookmark", "skip")
UNSORTED = "unsorted"


class Classifier:
    def __init__(self, triage_cfg: dict, warn=None):
        self._warn = warn or (lambda msg: print(msg, file=sys.stderr))
        self.bare_domain_is_bookmark = triage_cfg.get("bookmark_bare_domains", True)

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
            "url_rx": None,
            "title_rx": None,
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
        if not (rule["domains"] or rule["url_rx"] or rule["title_rx"]):
            # A rule with no conditions would match everything and silently
            # swallow the "unsorted" safety net.
            self._warn(f"Warning: category_rules[{i}] has no domains/url_regex/title_regex. Skipping rule.")
            return None
        return rule

    @staticmethod
    def _matches(rule, url: str, host: str, title: str) -> bool:
        # AND across the conditions a rule lists; absent conditions are ignored.
        if rule["domains"] and not any(d in host for d in rule["domains"]):
            return False
        if rule["url_rx"] and not rule["url_rx"].search(url):
            return False
        if rule["title_rx"] and not rule["title_rx"].search(title or ""):
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

    def classify(self, url: str, title: str):
        """Return (category, reason). reason explains the guess for --explain."""
        host = host_of(url)
        for r in self.skip_rules:
            if self._matches(r, url, host, title):
                return "skip", r["why"]
        if self.bare_domain_is_bookmark and self.is_bare_domain(url):
            return "bookmark", "site root (place, not thing)"
        for r in self.other_rules:
            if self._matches(r, url, host, title):
                return r["category"], r["why"]
        return UNSORTED, "no rule matched"
