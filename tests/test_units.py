"""Unit tests for the pure pieces: classifier, triage file, routing writers, readers."""

from __future__ import annotations

import os
import tempfile
import unittest
from html.parser import HTMLParser

import helpers  # noqa: F401  (puts lib/ on sys.path)
import collect
import route
import stg2gl_core as core
from classify import Classifier
from triage import Item, parse, render

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


RULES = {
    "category_rules": [
        {"category": "skip", "url_regex": "/login(/|$)"},
        {"category": "unsorted", "domains": ["docs.google.com"]},
        {"category": "note", "domains": ["stackoverflow.com"]},
        {"category": "bookmark", "domains": ["docs."]},
        {"category": "read", "domains": ["substack.com"]},
        {"category": "read", "url_regex": "/blog/"},
        {"category": "note", "title_regex": "\\brecipe\\b"},
    ]
}


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.c = Classifier(RULES, warn=lambda m: None)

    def cat(self, url, title=""):
        return self.c.classify(url, title)[0]

    def test_rules_and_fallback(self):
        self.assertEqual(self.cat("https://stackoverflow.com/questions/1/x"), "note")
        self.assertEqual(self.cat("https://docs.python.org/3/library/re.html"), "bookmark")
        self.assertEqual(self.cat("https://foo.substack.com/p/bar"), "read")
        self.assertEqual(self.cat("https://example.com/blog/post"), "read")
        self.assertEqual(self.cat("https://example.com/x", "Best Bread Recipe"), "note")
        self.assertEqual(self.cat("https://example.com/some/page"), "unsorted")

    def test_first_match_wins_and_unsorted_exception(self):
        # docs.google.com would match "docs." -> bookmark, but the earlier
        # unsorted rule wins.
        self.assertEqual(self.cat("https://docs.google.com/document/d/abc"), "unsorted")

    def test_skip_beats_bare_domain_and_bare_domain_beats_rules(self):
        self.assertEqual(self.cat("https://example.com/login"), "skip")
        # Bare substack root is a place (bookmark) even though substack -> read.
        self.assertEqual(self.cat("https://foo.substack.com/"), "bookmark")
        self.assertEqual(self.cat("https://example.com"), "bookmark")
        self.assertEqual(self.cat("https://example.com/#top"), "bookmark")
        self.assertEqual(self.cat("https://example.com/?q=1"), "unsorted")

    def test_bare_domain_rule_can_be_disabled(self):
        c = Classifier({"bookmark_bare_domains": False}, warn=lambda m: None)
        self.assertEqual(c.classify("https://example.com/", "")[0], "unsorted")

    def test_bad_rules_are_dropped_with_warning(self):
        msgs = []
        c = Classifier({"category_rules": [
            {"category": "nope", "domains": ["a"]},
            {"category": "read", "url_regex": "("},
            {"category": "read"},
        ]}, warn=msgs.append)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(c.other_rules, [])


class TriageFileTests(unittest.TestCase):
    def test_round_trip_with_awkward_characters(self):
        items = [
            Item("A [weird] title \\ with (parens)", "https://e.com/wiki/Foo_(bar)", "read", ["window", "dev"]),
            Item("Plain", "https://e.com/x", "unsorted", ["device/iphone"]),
            Item("", "https://e.com/no-title", "note", []),
        ]
        state, parsed, warnings = parse(render(items, "2026-11-20 10:00"))
        self.assertEqual(state, "pending")
        self.assertEqual(warnings, [])
        self.assertEqual(parsed[0].title, "Plain")  # unsorted renders first
        weird = [p for p in parsed if p.category == "read"][0]
        self.assertEqual(weird.title, "A [weird] title \\ with (parens)")
        self.assertEqual(weird.url, "https://e.com/wiki/Foo_%28bar%29")
        self.assertEqual(weird.tags, ["window", "dev"])
        # Empty title falls back to the URL as link text.
        self.assertEqual([p for p in parsed if p.category == "note"][0].title, "https://e.com/no-title")

    def test_moving_a_line_changes_category_and_comment_is_kept(self):
        text = render([Item("T", "https://e.com/a", "read", ["x"])], "now")
        line = "- [T](https://e.com/a) `x`"
        text = text.replace(line + "\n", "")
        text = text.replace("## note\n", "## note\n" + line + " why this matters\n")
        _s, parsed, _w = parse(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].category, "note")
        self.assertEqual(parsed[0].comment, "why this matters")

    def test_unknown_section_is_warned_not_guessed(self):
        text = "## later\n- [T](https://e.com/a)\n## read\n- [U](https://e.com/b)\n"
        _s, parsed, warnings = parse(text)
        self.assertEqual([p.url for p in parsed], ["https://e.com/b"])
        self.assertEqual(len(warnings), 2)


class CoreTests(unittest.TestCase):
    def test_url_key(self):
        self.assertEqual(core.url_key("https://e.com/a#x"), core.url_key("https://e.com/a"))
        self.assertEqual(core.url_key("https://e.com/F_(b)"), core.url_key("https://e.com/F_%28b%29"))
        self.assertNotEqual(core.url_key("https://e.com/a?x=1"), core.url_key("https://e.com/a"))

    def test_apply_rules_order_unchanged(self):
        r = core.TagRules({"domain_tags": {"github.com": ["dev"]},
                           "keyword_tags": [{"pattern": "rfc", "tags": ["rfc"]}]})
        self.assertEqual(r.apply_rules("https://github.com/rfc", "x", "My Group", ["Inbox"]),
                         ["tg/my-group", "inbox", "dev", "rfc"])


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs, self.folders = [], []
        self._in_h3 = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs)["href"])
        self._in_h3 = tag == "h3"

    def handle_data(self, data):
        if self._in_h3 and data.strip():
            self.folders.append(data.strip())
            self._in_h3 = False


class RouteTests(unittest.TestCase):
    def test_bookmarks_html_parses_and_escapes(self):
        items = [Item("A & <B>", 'https://e.com/?a=1&b="2"', "bookmark")]
        p = _Links()
        p.feed(route.bookmarks_html(items, "From tabs X"))
        self.assertEqual(p.hrefs, ['https://e.com/?a=1&b="2"'])
        self.assertEqual(p.folders, ["From tabs X"])

    def test_inboxes_append_and_never_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            note = os.path.join(d, "Tab inbox.md")
            with open(note, "w") as f:
                f.write("# Tab inbox\n\nhand-written line\n")
            route.append_note_inbox(note, [Item("T", "https://e.com/a", "note", ["window"], "because")], "2026-11-20")
            text = _read(note)
            self.assertTrue(text.startswith("# Tab inbox\n\nhand-written line\n"))
            self.assertIn("## 2026-11-20\n\n- [T](https://e.com/a) `window` because\n", text)

            os.mkdir(os.path.join(d, "bookmarks"))
            bm = os.path.join(d, "bookmarks", "Bookmarks inbox.md")
            route.append_bookmark_inbox(bm, [
                Item("Shop", "https://rei.com/", "bookmark", ["device/iphone", "gear"]),
                Item("Docs", "https://docs.x.org/", "bookmark", ["window"]),
            ], "2026-11-20")
            text = _read(bm)
            self.assertIn("# Bookmarks inbox", text)
            self.assertIn("### gear\n- [Shop](https://rei.com/)\n", text)
            self.assertIn("### other\n- [Docs](https://docs.x.org/)\n", text)

    def test_missing_folder_is_an_error_not_a_new_tree(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                route.append_note_inbox(os.path.join(d, "nope", "x.md"), [Item("T", "https://e.com", "note")], "d")

    def test_html_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            a = route.write_bookmarks_html(d, [Item("T", "https://e.com", "bookmark")], "s")
            b = route.write_bookmarks_html(d, [Item("T", "https://e.com", "bookmark")], "s")
            self.assertNotEqual(a, b)

    def test_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            led = route.Ledger(os.path.join(d, "l.db"))
            self.assertFalse(led.seen("https://e.com/a"))
            led.record("https://e.com/a#frag", "T", "read")
            led.record("https://e.com/a", "T", "note")  # replace, not duplicate
            self.assertTrue(led.seen("https://e.com/a#other"))
            self.assertEqual(led.counts(), {"note": 1})
            led.close()


class ReaderTests(unittest.TestCase):
    def test_window_lines(self):
        recs = collect.parse_window_lines("1\nA\nhttps://a\n2\n\nhttps://b\n")
        self.assertEqual([r["url"] for r in recs], ["https://a", "https://b"])
        self.assertEqual(recs[1]["title"], "")
        with self.assertRaises(collect.CollectError):
            collect.parse_window_lines("1\nA\n")

    def test_icloud_reader(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "CloudTabs.db")
            helpers.make_cloudtabs_db(db, {
                "Z's iPhone": [("A", "https://a.com/x"), ("B", "https://b.com/y")],
                "Old iPad": [("C", "https://c.com/z")],
            })
            recs = collect.from_icloud(db, exclude_devices=["old ipad"])
            self.assertEqual([r["url"] for r in recs], ["https://a.com/x", "https://b.com/y"])
            self.assertEqual(recs[0]["source"], "device/z's-iphone")

    def test_tab_groups_reader(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "SafariTabs.db")
            helpers.make_safaritabs_db(db, {
                "Research": [("R1", "https://r.com/1"), ("R2", "https://r.com/2")],
                "": [("unnamed", "https://u.com/1")],
                "Shopping": [("S1", "https://s.com/1")],
            })
            recs = collect.from_tab_groups_db(db, "tg/{group}", exclude_groups=["shopping"])
            self.assertEqual([(r["source"], r["url"]) for r in recs],
                             [("tg/research", "https://r.com/1"), ("tg/research", "https://r.com/2")])

    def test_schema_mismatch_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "CloudTabs.db")
            import sqlite3
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE cloud_tabs (id INTEGER)")
            conn.commit()
            conn.close()
            with self.assertRaises(collect.CollectError) as cm:
                collect.from_icloud(db)
            self.assertIn("schema may have changed", str(cm.exception))

    def test_missing_db_is_a_clear_error(self):
        with self.assertRaises(collect.CollectError):
            collect.from_icloud("/nonexistent/CloudTabs.db")


if __name__ == "__main__":
    unittest.main()
