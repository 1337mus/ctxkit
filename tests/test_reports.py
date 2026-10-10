import contextlib
import io
import json
import re
import sys
import time
import unittest
from datetime import date

import builders
from ctxkit import digest, distill
from ctxkit.corpus import Corpus
from ctxkit.doctor import classify_fat

TODAY = date(2026, 6, 1)


def capture(func, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = func(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


def headings(text):
    return re.findall(r"^## (.+)$", text, flags=re.MULTILINE)


class DigestTest(builders.TranscriptCase):
    def setUp(self):
        super().setUp()
        self.init_repo()

    def test_tripped_section_comes_first_and_says_so_when_nothing_is_wrong(self):
        text, tripped, summary = digest.build_digest(self.cfg, TODAY, time.time())
        self.assertEqual(tripped, [])
        self.assertEqual(headings(text)[0], "Tripped")
        self.assertIn("- Nothing tripped.", text)
        self.assertEqual(headings(text), ["Tripped", "Are agents using the corpus?", "Structure",
                                          "Gate friction, last 7 days (mentions in agent transcripts)"])
        self.assertIn("nothing tripped", summary)

    def test_every_tripped_condition_is_listed_before_anything_else(self):
        """Regression: a limit crossed for weeks went unnoticed because the digest only reported numbers."""
        self.load(top="fat_file_bytes = 1500\nindex_token_flag = 50")
        big = "storefront/search-ranking.md"
        small = self.read(big)
        self.write(big, small + "x" * 2000)
        classify_fat(Corpus(self.cfg))
        self.write(big, small)
        classify_fat(Corpus(self.cfg))
        self.write(big, small + "x" * 2000)
        self.set_age(big, 10)
        self.remove("storefront/promo-codes.md")                       # lost identifier
        self.write("storefront/stray.md", "# Stray\n\nUpdated: 2026-03-02\n")  # unreachable
        self.write("fulfillment/carrier-rates.md", self.read("fulfillment/carrier-rates.md") + "\nVerify by: 2020-01-01\n")
        text, tripped, summary = digest.build_digest(self.cfg, TODAY, time.time() + 3 * 86400 + 60)
        section = text.split("## Are agents")[0]
        for expected in ("Root index is about", "lost since the last commit", "unreachable from the root index",
                         "dormant regrown", "verify-by marker", "No corpus commit for 3 days"):
            self.assertIn(expected, section)
        self.assertEqual(len(tripped), 6)
        self.assertIn("6 condition(s) tripped", summary)

    def test_oversized_root_entry_is_a_tripped_condition(self):
        self.write("README.md", self.read("README.md") + "- [pasted](x.md): s\n" + "  filler text line\n" * 120)
        _, tripped, _ = digest.build_digest(self.cfg, TODAY, time.time())
        self.assertEqual(tripped, ["1 root index entry over 1500 bytes"])

    def test_week_over_week_delta_compares_with_a_row_at_least_seven_days_old(self):
        self.state.mkdir(parents=True)
        (self.state / "compliance.tsv").write_text(
            "2026-05-20\t10\t40.0\n2026-05-24\t12\t50.0\n2026-05-31\t14\t60.0\n2026-06-01\t16\t62.5\n")
        text, _, _ = digest.build_digest(self.cfg, TODAY, time.time())
        self.assertIn("**62.5%** of 16 substantial sessions (30d window, +12.5 vs a week ago)", text)

    def test_no_delta_is_claimed_when_there_is_no_old_enough_row(self):
        self.state.mkdir(parents=True)
        (self.state / "compliance.tsv").write_text("2026-05-31\t14\t60.0\n2026-06-01\t16\t62.5\n")
        text, _, _ = digest.build_digest(self.cfg, TODAY, time.time())
        self.assertIn("(30d window)", text)
        self.assertNotIn("vs a week ago", text)

    def test_missing_trend_explains_how_to_start_one(self):
        text, _, _ = digest.build_digest(self.cfg, TODAY, time.time())
        self.assertIn("No trend yet", text)

    def test_friction_counts_recent_mentions_from_both_tools_and_ignores_old_transcripts(self):
        noisy = builders.claude_result("ORPHANS (2): not reachable\nCONTENT LOST since the last commit")
        self.add_claude("recent", [noisy, noisy])
        self.add_codex("recent-codex", [json.dumps({"output": "ORPHANS (1)"})])
        self.add_claude("old", [noisy] * 5, age_days=20)
        counts = digest.friction_counts(self.cfg, time.time())
        self.assertEqual((counts["ORPHANS"], counts["CONTENT LOST"]), (3, 2))
        text, _, _ = digest.build_digest(self.cfg, TODAY, time.time())
        self.assertIn("- ORPHANS: 3", text)
        self.assertIn("include sessions that discussed a finding", text)

    def test_digest_is_written_to_a_dated_file_and_latest(self):
        code, out, _ = capture(digest.run, self.cfg, False, TODAY, time.time())
        self.assertEqual(code, 0)
        dated = self.state / "digest" / "2026-06-01.md"
        self.assertTrue(dated.is_file())
        self.assertEqual(dated.read_text(), (self.state / "digest" / "latest.md").read_text())
        self.assertIn(f"written: {dated}", out)

    def test_digest_directory_is_configurable(self):
        self.load(tables=f'[digest]\ndir = "{self.tmp / "reports"}"')
        capture(digest.run, self.cfg, False, TODAY, time.time())
        self.assertTrue((self.tmp / "reports" / "latest.md").is_file())

    def test_notify_runs_the_configured_command_with_path_and_summary(self):
        marker = self.tmp / "notified.txt"
        script = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '|' + sys.argv[3])"
        command = json.dumps([sys.executable, "-c", script, str(marker), "{path}", "{summary}"])
        self.load(tables=f"[digest]\nnotify_command = {command}")
        code, _, err = capture(digest.run, self.cfg, True, TODAY, time.time())
        self.assertEqual((code, err), (0, ""))
        path, summary = marker.read_text().split("|")
        self.assertTrue(path.endswith("2026-06-01.md"))
        self.assertIn("nothing tripped", summary)

    def test_notify_is_not_run_unless_asked(self):
        marker = self.tmp / "notified.txt"
        command = json.dumps([sys.executable, "-c", "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('x')", str(marker)])
        self.load(tables=f"[digest]\nnotify_command = {command}")
        capture(digest.run, self.cfg, False, TODAY, time.time())
        self.assertFalse(marker.exists())

    def test_notify_without_a_command_warns_and_a_failing_command_does_not_fail_the_digest(self):
        code, _, err = capture(digest.run, self.cfg, True, TODAY, time.time())
        self.assertEqual(code, 0)
        self.assertIn("notify_command is not configured", err)
        command = json.dumps([sys.executable, "-c", "import sys; sys.exit(3)"])
        self.load(tables=f"[digest]\nnotify_command = {command}")
        code, _, err = capture(digest.run, self.cfg, True, TODAY, time.time())
        self.assertEqual(code, 0)
        self.assertIn("exited 3", err)

    def test_missing_corpus_root_is_an_error(self):
        import shutil
        shutil.rmtree(self.root)
        code, _, err = capture(digest.run, self.cfg, False, TODAY, time.time())
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)


class DistillTest(builders.TranscriptCase):
    def test_brief_lists_recent_files_fat_candidates_target_and_prune_candidates(self):
        self.load(top="fat_file_bytes = 1000")
        for path in self.corpus.files():
            self.set_age(self.corpus.rel(path), 30)
        self.set_age("storefront/cart-checkout.md", 1)
        self.write("storefront/huge.md", "# Huge\n\nUpdated: 2026-03-02\n" + "x" * 1500)
        self.write("README.md", self.read("README.md") + "y" * 3000)
        self.add_codex("reader", [builders.codex_call(f"cat {self.root_path('fulfillment/carrier-rates.md')}")])
        code, out, _ = capture(distill.run, self.cfg, 7)
        self.assertEqual(code, 0)
        modified = out.split("SPLIT OR DISTILL")[0]
        self.assertIn("storefront/cart-checkout.md", modified)
        self.assertNotIn("fulfillment/returns-policy.md", modified)
        fat = out.split("SPLIT OR DISTILL")[1].split("PROMOTION TARGET")[0]
        self.assertIn("storefront/huge.md", fat)
        self.assertNotIn("README.md (", fat, "README files are exempt from the size ceiling")
        self.assertIn("PROMOTION TARGET: standing-rules.md", out)
        prune = out.split("PRUNE CANDIDATES")[1]
        self.assertIn("storefront/promo-codes.md", prune)
        self.assertNotIn("fulfillment/carrier-rates.md", prune.split("INSTRUCTIONS")[0],
                         "a file read by Codex is not a prune candidate")
        self.assertIn("ctx doctor", out)
        self.assertIn("standing-rules.md", out.split("INSTRUCTIONS")[1])

    def test_missing_promotion_target_is_stated_plainly(self):
        self.remove("standing-rules.md")
        _, out, _ = capture(distill.run, self.cfg, 7)
        self.assertIn("PROMOTION TARGET: standing-rules.md (missing)", out)

    def test_companion_standing_files_are_listed(self):
        self.load(top='standing_files = ["standing-rules.md", "standing-rules-extra.md"]')
        self.write("standing-rules-extra.md", "# Extra\n\nUpdated: 2026-03-02\n")
        _, out, _ = capture(distill.run, self.cfg, 7)
        self.assertIn("COMPANION STANDING FILE: standing-rules-extra.md", out)


if __name__ == "__main__":
    unittest.main()
