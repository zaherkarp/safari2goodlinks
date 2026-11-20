"""End-to-end runs of bin/stg2gl and bin/stg2gl-triage with fake osascript/open.

These exercise the real command-line entry points: argument parsing,
config loading, collect -> edit -> apply, the ledger across runs, and the
refactored bin/stg2gl heredoc importing lib/stg2gl_core.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

import helpers

STG2GL = os.path.join(helpers.REPO, "bin", "stg2gl")
TRIAGE = os.path.join(helpers.REPO, "bin", "stg2gl-triage")


class Env:
    """A temp sandbox: fake binaries, fixture DBs, a fake vault, and a rules
    file pointing every path into the sandbox."""

    def __init__(self, d, window_tabs, front_tabs=None, icloud=None, groups=None):
        self.d = d
        self.bin = os.path.join(d, "bin")
        self.open_log = helpers.install_fakes(self.bin, window_tabs, front_tabs)
        self.vault = os.path.join(d, "vault")
        os.makedirs(os.path.join(self.vault, "bookmarks"))
        self.downloads = os.path.join(d, "Downloads")
        os.makedirs(self.downloads)
        self.triage = os.path.join(d, "triage", "triage.md")
        cloud = os.path.join(d, "CloudTabs.db")
        tabs_db = os.path.join(d, "SafariTabs.db")
        helpers.make_cloudtabs_db(cloud, icloud or {})
        helpers.make_safaritabs_db(tabs_db, groups or {})

        with open(os.path.join(helpers.REPO, "config", "rules.json")) as f:
            cfg = json.load(f)
        cfg["triage"].update({
            "vault_path": self.vault,
            "triage_file": self.triage,
            "ledger": os.path.join(d, "ledger.db"),
            "bookmarks_html_dir": self.downloads,
        })
        cfg["triage"]["sources"]["tab_groups"]["db_path"] = tabs_db
        cfg["triage"]["sources"]["icloud"]["db_path"] = cloud
        cfg["throttle_ms"] = 0
        self.rules = os.path.join(d, "rules.json")
        with open(self.rules, "w") as f:
            json.dump(cfg, f)
        self.env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"])

    def run(self, *args, cmd=TRIAGE):
        pre = ["--rules", self.rules] if cmd == TRIAGE else ["--rules", self.rules]
        return subprocess.run([cmd, *pre, *args], capture_output=True, text=True, env=self.env, cwd=self.d)

    def read(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()


WINDOW_TABS = [
    (1, "Some Essay", "https://writer.substack.com/p/some-essay"),
    (1, "python - How to x - Stack Overflow", "https://stackoverflow.com/questions/1/how-to-x"),
    (1, "REI", "https://www.rei.com/"),
    (2, "Sign in", "https://example.com/login"),
    (2, "Odd page", "https://example.com/some/page"),
    (2, "Local", "http://localhost:3000/"),          # skipped by skip_url_regex
    (2, "Some Essay (dup)", "https://writer.substack.com/p/some-essay#comments"),
]


class TriageE2E(unittest.TestCase):
    def test_collect_edit_apply_and_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, WINDOW_TABS,
                    icloud={"Z's iPhone": [("Paper", "https://arxiv.org/abs/2401.00001")]},
                    groups={"Research": [("Talk", "https://www.youtube.com/watch?v=abc")]})

            # --- collect ------------------------------------------------
            r = e.run("collect")
            self.assertEqual(r.returncode, 0, r.stderr)
            text = e.read(e.triage)
            self.assertIn("state=pending", text)
            # Duplicate (same page, #fragment) merged into one line.
            self.assertEqual(text.count("writer.substack.com/p/some-essay"), 1)
            self.assertNotIn("localhost", text)
            self.assertIn("`device/z's-iphone`", text)
            self.assertIn("`tg/research`", text)
            self.assertIn("Duplicates merged:   1", r.stderr)

            # A second collect must refuse to clobber unapplied edits.
            r = e.run("collect")
            self.assertEqual(r.returncode, 1)
            self.assertIn("not applied yet", r.stderr)

            # --- edit: move the unsorted page to "note" with a comment ----
            line = "- [Odd page](https://example.com/some/page) `window`"
            self.assertIn(line, text)
            text = text.replace(line + "\n", "")
            text = text.replace("## note\n", "## note\n" + line + " check the table\n")
            with open(e.triage, "w") as f:
                f.write(text)

            # --- apply ----------------------------------------------------
            r = e.run("apply")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("status=OK", r.stderr)

            gl = e.read(e.open_log)
            self.assertIn("-Ra GoodLinks", gl)
            self.assertIn("goodlinks://x-callback-url/save", gl)
            self.assertIn("arxiv.org", gl)       # read: paper rule
            self.assertIn("youtube.com", gl)     # read: video rule

            note = e.read(e.vault, "Tab inbox.md")
            self.assertIn("stackoverflow.com/questions/1/how-to-x", note)
            self.assertIn("[Odd page](https://example.com/some/page) `window` check the table", note)

            bm = e.read(e.vault, "bookmarks", "Bookmarks inbox.md")
            self.assertIn("[REI](https://www.rei.com/)", bm)
            html_files = os.listdir(e.downloads)
            self.assertEqual(len(html_files), 1)
            self.assertIn("https://www.rei.com/", e.read(e.downloads, html_files[0]))

            self.assertIn("state=applied", e.read(e.triage))

            # --- second collect: everything routed is hidden ---------------
            r = e.run("collect")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Already routed:      7", r.stderr)
            self.assertNotIn("](https://", e.read(e.triage))  # no item lines left

            r = e.run("status")
            self.assertIn("total=7", r.stdout)

    def test_apply_failure_keeps_items_for_retry(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, WINDOW_TABS)
            self.assertEqual(e.run("collect").returncode, 0)
            # Break the bookmark destination: remove the vault's bookmarks/ folder.
            os.rmdir(os.path.join(e.vault, "bookmarks"))
            r = e.run("apply")
            self.assertEqual(r.returncode, 2)
            self.assertIn("status=PARTIAL_FAILURE", r.stderr)
            left = e.read(e.triage)
            self.assertIn("state=pending", left)
            self.assertIn("rei.com", left)           # failed bookmark kept
            self.assertNotIn("stackoverflow", left)  # routed note removed

    def test_missing_source_is_a_warning_not_a_failure(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, WINDOW_TABS)
            os.remove(os.path.join(d, "CloudTabs.db"))
            r = e.run("collect")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Warning: icloud: not found", r.stderr)

    def test_dry_runs_write_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, WINDOW_TABS)
            r = e.run("collect", "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("why: Q&A answer", r.stdout)
            self.assertFalse(os.path.exists(e.triage))
            e.run("collect")
            r = e.run("apply", "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(e.open_log))
            self.assertFalse(os.path.exists(os.path.join(e.vault, "Tab inbox.md")))


class Stg2glRefactorE2E(unittest.TestCase):
    """bin/stg2gl must behave as before now that its rules live in lib/."""

    FRONT = [
        {"title": "GitHub - x/y", "url": "https://github.com/x/y"},
        {"title": "RFC 9110", "url": "https://www.rfc-editor.org/rfc/rfc9110"},
        {"title": "about", "url": "about:blank"},
    ]

    def test_dry_run_and_json_output(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, [], front_tabs=self.FRONT)
            r = e.run("--mode", "active", "--dry-run", "--base-tag", "Inbox", cmd=STG2GL)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("tags: tg/active inbox dev", r.stdout)
            self.assertIn("tags: tg/active inbox rfc", r.stdout)
            self.assertIn("Done. total=3 saved=2 skipped=1 failed=0 status=OK", r.stderr)

            out = os.path.join(d, "out.json")
            r = e.run("--mode", "active", "--output", "json", "--output-file", out, cmd=STG2GL)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(out) as f:
                data = json.load(f)
            self.assertEqual([x["url"] for x in data], ["https://github.com/x/y", "https://www.rfc-editor.org/rfc/rfc9110"])

    def test_goodlinks_output_uses_open(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, [], front_tabs=self.FRONT)
            r = e.run("--mode", "active", cmd=STG2GL)
            self.assertEqual(r.returncode, 0, r.stderr)
            log = e.read(e.open_log)
            self.assertIn("-Ra GoodLinks", log)
            self.assertEqual(log.count("goodlinks://x-callback-url/save"), 2)


if __name__ == "__main__":
    unittest.main()
