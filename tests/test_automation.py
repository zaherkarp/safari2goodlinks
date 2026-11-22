"""Tests for the automation layer: auto-send, closing tabs, learning,
page labels, scheduling and notifications.

Unit tests first, then end-to-end runs through bin/stg2gl-triage with the
fake osascript / open / launchctl from helpers.py.
"""

from __future__ import annotations

import http.server
import json
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import threading
import unittest

import helpers
import hints
import learn
import route
import schedule
from classify import Classifier
from test_e2e import TRIAGE, Env
from triage import Item, parse_full, render, sent_line


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class ClassifierAutomationTests(unittest.TestCase):
    CFG = {"category_rules": [
        {"category": "read", "domains": ["arxiv.org"], "auto": True},
        {"category": "unsorted", "domains": ["docs.google.com"], "auto": True},
        {"category": "note", "page": ["recipe"], "why": "recipe page"},
        {"category": "read", "domains": ["substack.com"]},
    ]}

    def c(self, learned=()):
        return Classifier(self.CFG, learned=learned, warn=lambda m: None)

    def test_auto_flag(self):
        self.assertTrue(self.c().classify("https://arxiv.org/abs/1", "").auto)
        self.assertFalse(self.c().classify("https://x.substack.com/p/a", "").auto)
        # "auto" on an unsorted rule is ignored.
        self.assertFalse(self.c().classify("https://docs.google.com/d/1", "").auto)

    def test_page_condition_needs_labels(self):
        c = self.c()
        self.assertEqual(c.classify("https://food.example/x", "").category, "unsorted")
        g = c.classify("https://food.example/x", "", ["window", "page:recipe"])
        self.assertEqual((g.category, g.why), ("note", "recipe page"))

    def test_learned_rules_come_after_config_and_never_auto(self):
        learned = [("seriouseats.com", "note", "learned: x"), ("substack.com", "note", "learned: y")]
        c = self.c(learned)
        g = c.classify("https://www.seriouseats.com/best-thing", "")
        self.assertEqual((g.category, g.auto), ("note", False))
        # Subdomain of a learned site matches too.
        self.assertEqual(c.classify("https://m.seriouseats.com/a", "").category, "note")
        # Config rule (substack -> read) beats a learned rule.
        self.assertEqual(c.classify("https://x.substack.com/p/a", "").category, "read")


class LearnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = route.Ledger(os.path.join(self.tmp.name, "l.db"))

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def decide(self, url, guess, final, why="no rule matched"):
        self.led.remember_guess(url, guess, why)
        self.led.record_decision(url, final)

    def test_learns_after_min_count_and_unlearns_when_inconsistent(self):
        for i in range(2):
            self.decide(f"https://www.site.com/{i}", "unsorted", "note")
        self.assertEqual(learn.learned_rules(self.led.conn), [])
        self.decide("https://site.com/2", "unsorted", "note")
        rules = learn.learned_rules(self.led.conn)
        self.assertEqual([(s, c) for s, c, _ in rules], [("site.com", "note")])
        self.assertIn("3 of 3", rules[0][2])
        # One move elsewhere: 3/4 = 75% < 80%, rule disappears.
        self.decide("https://site.com/3", "note", "read", why=rules[0][2])
        self.assertEqual(learn.learned_rules(self.led.conn), [])
        # forget wipes the site entirely.
        self.assertEqual(self.led.forget_site("site.com"), 4)

    def test_guess_is_consumed_and_missing_guess_is_unsorted(self):
        self.decide("https://a.com/x", "read", "read")
        self.led.record_decision("https://b.com/y", "note")  # no stored guess
        rows = self.led.conn.execute("SELECT site, guessed, final FROM decisions ORDER BY id").fetchall()
        self.assertEqual(rows, [("a.com", "read", "read"), ("b.com", "unsorted", "note")])
        self.assertEqual(self.led.conn.execute("SELECT COUNT(*) FROM guesses").fetchone()[0], 0)

    def test_overridden_rules_and_health(self):
        for i in range(3):
            self.decide(f"https://w{i}.org/x", "note", "read", why="encyclopedia entry")
        self.decide("https://ok.org/x", "note", "note", why="encyclopedia entry")
        over = learn.overridden_rules(self.led.conn)
        self.assertEqual([tuple(r) for r in over], [("encyclopedia entry", "note", "read", 3)])
        for _ in range(3):
            self.led.record_run("collect", 10, 6, 0, 0)
        h = learn.sorting_health(self.led.conn)
        self.assertAlmostEqual(h["unsorted_share"], 0.6)
        self.assertAlmostEqual(h["correction_share"], 0.75)
        self.assertIn("matched no rule", learn.page_hint_advice(h, "off"))
        self.assertIsNone(learn.page_hint_advice(h, "unsorted"))  # already on
        self.assertIsNone(learn.page_hint_advice(dict(h, classified=5), "off"))  # too little data


class HintParserTests(unittest.TestCase):
    def test_recipe_in_graph_ignores_publisher(self):
        html = """<html><head>
        <script type="application/ld+json">{"@context":"https://schema.org","@graph":[
          {"@type":"WebSite","name":"x"},
          {"@type":["Recipe"],"name":"Bread","publisher":{"@type":"Organization"}}]}</script>
        </head><body></body></html>"""
        self.assertEqual(hints.labels_from_html(html), ["page:recipe"])

    def test_og_type_feed_and_reading_time(self):
        body = "<p>" + "word " * 2300 + "</p>"
        html = f"""<head><meta property="og:type" content="article">
        <link rel="alternate" type="application/rss+xml" href="/feed">
        <script>var notcounted = "{'x ' * 5000}";</script></head><body>{body}</body>"""
        self.assertEqual(hints.labels_from_html(html), ["page:article", "page:feed", "read:10min"])

    def test_bad_json_and_unknown_types_are_ignored(self):
        html = """<script type="application/ld+json">{not json</script>
        <script type="application/ld+json">{"@type":"Organization"}</script>
        <meta property="og:type" content="website">"""
        self.assertEqual(hints.labels_from_html(html), [])


class _Handler(http.server.BaseHTTPRequestHandler):
    PAGES = {
        "/recipe": ("text/html; charset=utf-8",
                    b'<script type="application/ld+json">{"@type":"Recipe"}</script>'),
        "/paper.pdf": ("application/pdf", b"%PDF-1.4"),
        "/data.json": ("application/json", b"{}"),
    }

    def do_GET(self):
        page = self.PAGES.get(self.path)
        if not page:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", page[0])
        self.end_headers()
        self.wfile.write(page[1])

    def log_message(self, *a):  # keep test output quiet
        pass


class LocalServer:
    """A throwaway HTTP server on 127.0.0.1 for fetch tests."""

    def __enter__(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


class HintFetchTests(unittest.TestCase):
    def test_fetch_many(self):
        # Make sure an inherited proxy setting does not route 127.0.0.1 away.
        old = os.environ.get("NO_PROXY")
        os.environ["NO_PROXY"] = "127.0.0.1"
        try:
            with LocalServer() as s:
                res = hints.fetch_many([s.base + p for p in ("/recipe", "/paper.pdf", "/data.json", "/missing")])
        finally:
            if old is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = old
        labels = {u.rsplit("/", 1)[1]: v for u, v in res.items()}
        self.assertEqual(labels["recipe"], (["page:recipe"], None))
        self.assertEqual(labels["paper.pdf"], (["page:pdf"], None))
        self.assertEqual(labels["data.json"][0], [])
        self.assertIn("not a web page", labels["data.json"][1])
        self.assertEqual(labels["missing"][0], [])
        self.assertIsNotNone(labels["missing"][1])
        self.assertEqual(hints.fetch_labels("ftp://x"), ([], "not http(s)"))


class ScheduleTests(unittest.TestCase):
    def test_parse_when(self):
        self.assertEqual(schedule.parse_when(daily="18:30"), [{"Hour": 18, "Minute": 30}])
        self.assertEqual(schedule.parse_when(weekly="mon,thu 07:05"),
                         [{"Weekday": 1, "Hour": 7, "Minute": 5}, {"Weekday": 4, "Hour": 7, "Minute": 5}])
        self.assertEqual(schedule.parse_when(weekly="Sunday 09:00")[0]["Weekday"], 0)
        for bad in [dict(), dict(daily="9:00", weekly="sun 9:00"), dict(daily="25:00"),
                    dict(daily="noon"), dict(weekly="someday 09:00"), dict(weekly="09:00")]:
            with self.assertRaises(schedule.ScheduleError, msg=bad):
                schedule.parse_when(**bad)

    def test_plist(self):
        pl = schedule.build_plist("/usr/bin/python3", "/r/bin/stg2gl-triage", "/r/rules.json", [{"Hour": 9, "Minute": 0}])
        self.assertEqual(pl["ProgramArguments"], ["/usr/bin/python3", "/r/bin/stg2gl-triage", "--rules", "/r/rules.json", "auto"])
        self.assertIn("/opt/homebrew/bin", pl["EnvironmentVariables"]["PATH"])
        plistlib.dumps(pl)  # serialisable


class RouteAutomationTests(unittest.TestCase):
    def test_tabs_to_close(self):
        items = [Item("a", "https://a.com/x#frag", "read"), Item("b", "https://mail.google.com/", "skip"),
                 Item("c", "https://keep.me/y", "note"), Item("d", "https://d.com/F_(1)", "bookmark")]
        cfg = {"close_tabs": True, "close_tabs_keep_domains": ["keep.me"]}
        self.assertEqual(route.tabs_to_close(items, cfg), ["https://a.com/x", "https://d.com/F_%281%29"])
        self.assertEqual(route.tabs_to_close(items, {"close_tabs": False}), [])

    def test_sent_section_round_trip(self):
        sent = [sent_line(Item("P [1]", "https://arxiv.org/abs/1", "read"), "paper")]
        text = render([Item("T", "https://e.com/a", "unsorted")], "now", sent=sent)
        state, items, warnings, got = parse_full(text)
        self.assertEqual((state, len(items), warnings), ("pending", 1, []))
        self.assertEqual(got, ["- read: [P \\[1\\]](https://arxiv.org/abs/1) (paper)"])


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

TABS = [
    (1, "A paper", "https://arxiv.org/abs/2401.00001"),               # read, auto
    (1, "How to x - Stack Overflow", "https://stackoverflow.com/questions/9/x"),  # note, auto
    (1, "Inbox", "https://mail.google.com/mail/u/0/"),                # skip, auto, never closed
    (1, "Essay", "https://writer.substack.com/p/essay"),              # read, NOT auto
    (2, "Odd", "https://odd.example/a/b"),                            # unsorted
]


class AutoE2E(unittest.TestCase):
    def test_auto_sends_closes_and_notifies(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, TABS)
            r = e.run("auto")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Sent automatically:  3", r.stderr)

            gl = _read(e.open_log)
            self.assertIn("arxiv.org", gl)
            self.assertNotIn("substack", gl)  # not an auto rule: waits for review

            self.assertIn("stackoverflow.com/questions/9/x", _read(os.path.join(e.vault, "Tab inbox.md")))

            # Closed: the paper and the Q&A tab. Never the skipped webmail.
            closed = _read(os.path.join(e.bin, "close.log")).split()
            self.assertEqual(sorted(closed), ["https://arxiv.org/abs/2401.00001",
                                              "https://stackoverflow.com/questions/9/x"])
            self.assertIn("Tabs closed:         2", r.stderr)

            text = _read(e.triage)
            self.assertIn("## sent automatically", text)
            self.assertIn("- read: [A paper](https://arxiv.org/abs/2401.00001) (paper)", text)
            state, items, _w, sent = parse_full(text)
            self.assertEqual({(it.category, it.url) for it in items},
                             {("read", "https://writer.substack.com/p/essay"), ("unsorted", "https://odd.example/a/b")})
            self.assertEqual(len(sent), 3)

            self.assertIn("Tab triage | 2 tabs to sort, 3 sent automatically, 2 closed",
                          _read(os.path.join(e.bin, "notify.log")))

            # Apply clears the sent list and closes what it routes.
            r = e.run("apply")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("sent automatically", _read(e.triage))
            self.assertIn("https://writer.substack.com/p/essay", _read(os.path.join(e.bin, "close.log")))

    def test_auto_with_nothing_new_is_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, [])
            r = e.run("auto")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(e.bin, "notify.log")))

    def test_auto_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, TABS)
            r = e.run("auto", "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("[read     AUTO]", r.stdout)
            for f in ("close.log", "notify.log", "open.log"):
                self.assertFalse(os.path.exists(os.path.join(e.bin, f)), f)
            self.assertFalse(os.path.exists(e.triage))

    def test_close_tabs_off(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, TABS)
            cfg = json.loads(_read(e.rules))
            cfg["triage"]["close_tabs"] = False
            with open(e.rules, "w") as f:
                json.dump(cfg, f)
            self.assertEqual(e.run("auto").returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(e.bin, "close.log")))


class LearningE2E(unittest.TestCase):
    def test_three_moves_teach_a_rule(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, [])
            for i in range(3):
                helpers.set_window_tabs(e.bin, [(1, f"Bread {i}", f"https://www.kitchen.example/x/{i}")])
                self.assertEqual(e.run("collect").returncode, 0)
                text = _read(e.triage)
                line = f"- [Bread {i}](https://www.kitchen.example/x/{i}) `window`"
                self.assertIn(line, text.split("## read")[0])  # guessed unsorted
                text = text.replace(line + "\n", "").replace("## note\n", "## note\n" + line + "\n")
                with open(e.triage, "w") as f:
                    f.write(text)
                self.assertEqual(e.run("apply").returncode, 0)

            helpers.set_window_tabs(e.bin, [(1, "Bread 4", "https://kitchen.example/x/4")])
            r = e.run("collect", "--dry-run")
            self.assertIn("[note    ]", r.stdout)
            self.assertIn("learned: you put 3 of 3 kitchen.example tabs in note", r.stdout)

            r = e.run("status")
            self.assertIn("kitchen.example", r.stdout)
            self.assertIn("-> note", r.stdout)

            r = e.run("forget", "kitchen.example")
            self.assertIn("Forgot 3 decisions", r.stdout)
            r = e.run("collect", "--dry-run")
            self.assertIn("[unsorted]", r.stdout)


class PageLabelsE2E(unittest.TestCase):
    def test_labels_added_and_rule_can_use_them(self):
        with tempfile.TemporaryDirectory() as d, LocalServer() as s:
            e = Env(d, [(1, "Bread", s.base + "/recipe"), (1, "Paper", s.base + "/paper.pdf")])
            e.env["NO_PROXY"] = "127.0.0.1"
            cfg = json.loads(_read(e.rules))
            cfg["triage"]["page_hints"] = "unsorted"
            cfg["triage"]["category_rules"].insert(0, {"category": "note", "page": ["recipe"], "why": "recipe page"})
            with open(e.rules, "w") as f:
                json.dump(cfg, f)
            r = e.run("collect")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Page labels added:   2", r.stderr)
            _s, items, _w, _sent = parse_full(_read(e.triage))
            by = {it.url.rsplit("/", 1)[1]: it for it in items}
            self.assertEqual(by["recipe"].category, "note")     # rule opted in
            self.assertIn("page:recipe", by["recipe"].tags)
            self.assertEqual(by["paper.pdf"].category, "unsorted")  # label only
            self.assertIn("page:pdf", by["paper.pdf"].tags)


class ScheduleE2E(unittest.TestCase):
    def test_install_show_remove(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, [])
            e.env["HOME"] = d
            r = e.run("schedule", "install", "--weekly", "sun 09:00")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Full Disk Access", r.stdout)
            plist = os.path.join(d, "Library", "LaunchAgents", "com.stg2gl.triage.plist")
            with open(plist, "rb") as f:
                pl = plistlib.load(f)
            self.assertEqual(pl["StartCalendarInterval"], [{"Weekday": 0, "Hour": 9, "Minute": 0}])
            self.assertEqual(pl["ProgramArguments"][1:], [TRIAGE, "--rules", os.path.realpath(e.rules), "auto"])
            calls = _read(os.path.join(e.bin, "launchctl.log"))
            self.assertIn("bootstrap gui/", calls)

            self.assertIn("sun 09:00  (loaded)", e.run("schedule", "show").stdout)
            self.assertEqual(e.run("schedule", "run-now").returncode, 0)
            self.assertIn("kickstart -k gui/", _read(os.path.join(e.bin, "launchctl.log")))
            self.assertIn("Removed.", e.run("schedule", "remove").stdout)
            self.assertFalse(os.path.exists(plist))

            r = e.run("schedule", "install")
            self.assertEqual(r.returncode, 1)
            self.assertIn("exactly one of", r.stderr)


class LockE2E(unittest.TestCase):
    def test_second_run_is_refused_while_locked(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, TABS)
            lock = open(os.path.join(d, "lock"), "w")  # ledger lives in d
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                r = e.run("collect")
            finally:
                lock.close()
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("in progress", r.stderr)


if __name__ == "__main__":
    unittest.main()
