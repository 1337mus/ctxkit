import contextlib
import io
import json
import unittest
from datetime import date

import support
from ctxkit import doctor
from ctxkit.corpus import Corpus
from ctxkit.doctor import (check_expired, check_root_index, classify_fat, fat_candidates,
                           format_findings, run_checks)


def run_doctor(cfg):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = doctor.run(cfg)
    return code, out.getvalue(), err.getvalue()


class ExpiredMarkerTest(support.WorkspaceCase):
    def test_marker_past_its_date_is_reported_with_days_overdue(self):
        self.write("storefront/promo-codes.md",
                   "# Promo codes\n\nUpdated: 2026-03-02\n\nVerify by: 2026-04-01 the loyalty rule still holds.\n")
        found = check_expired(self.corpus, today=date(2026, 6, 1))
        self.assertEqual([(e.path, e.due, e.days_overdue) for e in found],
                         [("storefront/promo-codes.md", "2026-04-01", 61)])

    def test_marker_on_or_after_today_is_not_reported(self):
        """Regression: re-verifying and pushing the date out is the only suppression, and it must work."""
        self.write("storefront/promo-codes.md", "# P\n\nUpdated: 2026-03-02\n\nVerify by: 2026-06-01\n")
        self.assertEqual(check_expired(self.corpus, today=date(2026, 6, 1)), [])
        self.assertEqual(check_expired(self.corpus, today=date(2026, 5, 31)), [])

    def test_archived_files_are_exempt_but_the_same_marker_elsewhere_is_not(self):
        """Regression: finished work is a historical record, its old claims are not review prompts."""
        marker = "# Old\n\nUpdated: 2026-03-02\n\nVerify by: 2026-04-01\n"
        self.write("fulfillment/archive/warehouse-migration.md", marker)
        self.assertEqual(check_expired(self.corpus, today=date(2026, 6, 1)), [])
        self.write("fulfillment/carrier-rates.md", marker)
        self.assertEqual([e.path for e in check_expired(self.corpus, today=date(2026, 6, 1))],
                         ["fulfillment/carrier-rates.md"])

    def test_a_later_overdue_marker_is_found_even_when_an_earlier_one_is_still_in_the_future(self):
        """Regression: only looking at the first marker in a file hides every claim after it."""
        self.write("storefront/promo-codes.md",
                   "# P\n\nUpdated: 2026-03-02\n\nVerify by: 2026-12-01\n\nVerify by: 2026-04-01\n")
        found = check_expired(self.corpus, today=date(2026, 6, 1))
        self.assertEqual([(e.due, e.days_overdue) for e in found], [("2026-04-01", 61)])

    def test_impossible_dates_are_ignored_instead_of_crashing(self):
        self.write("storefront/promo-codes.md", "# P\n\nUpdated: 2026-03-02\n\nVerify by: 2026-13-45\n")
        self.assertEqual(check_expired(self.corpus, today=date(2026, 6, 1)), [])


class FatLifecycleTest(support.WorkspaceCase):
    """A distilled file that regrows fails only once it has gone quiet, and stays armed until it shrinks."""

    def setUp(self):
        super().setUp()
        self.load(top="fat_file_bytes = 2000\nregrown_dormant_days = 7")
        self.big = "storefront/search-ranking.md"
        self.small_text = self.read(self.big)

    def grow(self, size=3000):
        self.write(self.big, self.small_text + "x" * size + "\n")

    def history(self):
        return json.loads((self.state / "fat-history.json").read_text())

    def classify(self):
        return classify_fat(Corpus(self.cfg))

    def test_first_time_over_the_ceiling_is_advisory_only(self):
        self.grow()
        first, regrown = self.classify()
        self.assertEqual([f.path for f in first], [self.big])
        self.assertEqual(regrown, [])
        self.assertEqual(self.history()[self.big]["distilled"], False)

    def test_shrinking_below_the_ceiling_arms_the_check(self):
        self.grow()
        self.classify()
        self.write(self.big, self.small_text)
        first, regrown = self.classify()
        self.assertEqual((first, regrown), ([], []))
        self.assertEqual(self.history()[self.big]["distilled"], True)

    def test_regrown_but_still_being_written_stays_advisory(self):
        """Regression: failing a busy file forces a split every few days, which punishes active work."""
        self.grow()
        self.classify()
        self.write(self.big, self.small_text)
        self.classify()
        self.grow()
        first, regrown = self.classify()
        self.assertEqual([f.path for f in first], [self.big])
        self.assertEqual(regrown, [])
        self.assertEqual(self.history()[self.big]["distilled"], True, "hot regrowth must not disarm the check")

    def test_regrown_and_dormant_fails_and_keeps_failing_across_runs(self):
        """Regression: the check once reported once, disarmed itself, and every later run was a soft advisory."""
        self.grow()
        self.classify()
        self.write(self.big, self.small_text)
        self.classify()
        self.grow()
        self.set_age(self.big, 10)
        for run_number in (1, 2, 3):
            first, regrown = self.classify()
            self.assertEqual([f.path for f in regrown], [self.big], f"run {run_number}")
            self.assertEqual(first, [], f"run {run_number}")
            self.assertEqual(self.history()[self.big]["distilled"], True, f"run {run_number}")

    def test_dormant_file_that_was_never_distilled_is_only_advisory(self):
        """Regression: the first crossing is a backlog item, not a failure, however quiet the file is."""
        self.grow()
        self.set_age(self.big, 30)
        first, regrown = self.classify()
        self.assertEqual(([f.path for f in first], regrown), ([self.big], []))

    def test_activity_after_dormancy_downgrades_to_advisory_again(self):
        self.grow()
        self.classify()
        self.write(self.big, self.small_text)
        self.classify()
        self.grow()
        self.set_age(self.big, 10)
        self.assertEqual(len(self.classify()[1]), 1)
        self.grow(3100)  # a fresh write makes the file hot again
        first, regrown = self.classify()
        self.assertEqual((len(first), regrown), (1, []))

    def test_readmes_and_archived_files_are_exempt_from_the_ceiling(self):
        self.write("storefront/README.md", self.read("storefront/README.md") + "y" * 5000)
        self.write("fulfillment/archive/warehouse-migration.md",
                   self.read("fulfillment/archive/warehouse-migration.md") + "z" * 5000)
        self.assertEqual(self.classify(), ([], []))
        self.assertEqual(fat_candidates(Corpus(self.cfg)), [])

    def test_a_corrupt_history_file_degrades_to_first_time_behaviour(self):
        self.state.mkdir(parents=True)
        (self.state / "fat-history.json").write_text("{not json")
        self.grow()
        first, regrown = self.classify()
        self.assertEqual(([f.path for f in first], regrown), ([self.big], []))

    def test_doctor_exit_code_is_zero_while_hot_and_one_once_dormant(self):
        self.init_repo()
        self.grow()
        run_doctor(self.cfg)
        self.write(self.big, self.small_text)
        run_doctor(self.cfg)
        self.grow()
        code, out, _ = run_doctor(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("ADVISORY", out)
        self.set_age(self.big, 10)
        code, out, _ = run_doctor(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("REGROWN and now DORMANT", out)


class FatAdvisoryOutputTest(support.WorkspaceCase):
    def test_lists_only_recent_files_and_counts_the_rest(self):
        """Regression: printing every fat file at every run trains readers to skim past the real failures."""
        self.load(top="fat_file_bytes = 1000")
        names = [f"storefront/big-{i}.md" for i in range(9)]
        for i, rel in enumerate(names):
            self.write(rel, f"# Big {i}\n\nUpdated: 2026-03-02\n\n" + "w" * 1200 + "\n")
        for rel in names[6:]:
            self.set_age(rel, 10)
        findings = run_checks(Corpus(self.cfg))
        output = format_findings(findings, self.cfg)
        start = next(i for i, line in enumerate(output) if line.startswith("ADVISORY"))
        section = output[start + 1:]
        listed = [line for line in section if "written recently" in line]
        self.assertEqual(len(listed), 5)
        self.assertTrue(all("big-" in line for line in listed))
        self.assertIn("  ...and 4 more. Full list: ctx distill", section)


class RootBudgetTest(support.WorkspaceCase):
    def test_fixture_root_is_within_budget(self):
        report = check_root_index(self.corpus)
        self.assertFalse(report.over_budget)

    def test_single_oversized_entry_fails_even_when_the_total_is_small(self):
        """Regression: a whole topic pasted under one bullet makes the index a place to read, not route."""
        pasted = "".join(f"  detail line {i} that belongs in a topic file\n" for i in range(60))
        self.write("README.md", self.read("README.md") + "- [pasted](storefront/README.md): summary\n" + pasted)
        report = check_root_index(Corpus(self.cfg))
        self.assertTrue(report.over_budget)
        self.assertFalse(report.over_tokens)
        self.assertEqual(len(report.over_entries), 1)
        self.assertTrue(report.over_entries[0][0].startswith("[pasted]"))
        self.assertGreater(report.over_entries[0][1], 1500)

    def test_long_prose_under_a_heading_is_not_an_entry(self):
        """Regression: only bullets are entries, so an intro paragraph cannot trip the entry limit."""
        self.write("README.md", self.read("README.md") + "\n## Notes\n\n" + ("lorem ipsum " * 400) + "\n")
        self.assertEqual(check_root_index(Corpus(self.cfg)).over_entries, [])

    def test_a_new_bullet_ends_the_previous_entry(self):
        many = "".join(f"- [f{i}.md](f{i}.md): short pointer number {i}\n" for i in range(80))
        self.write("README.md", self.read("README.md") + many)
        self.assertEqual(check_root_index(Corpus(self.cfg)).over_entries, [])

    def test_total_size_over_the_token_flag_fails(self):
        filler = "".join(f"- [t{i}.md](t{i}.md): pointer number {i} with a few words of routing text\n" for i in range(560))
        self.write("README.md", self.read("README.md") + filler)
        report = check_root_index(Corpus(self.cfg))
        self.assertGreater(report.tokens, 10000)
        self.assertTrue(report.over_tokens and report.over_budget)
        self.assertEqual(report.over_entries, [])

    def test_thresholds_come_from_configuration(self):
        self.load(top="index_token_flag = 50\nindex_entry_max_bytes = 100")
        report = check_root_index(Corpus(self.cfg))
        self.assertTrue(report.over_tokens)
        self.assertTrue(report.over_entries)


class DoctorCommandTest(support.WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.init_repo()

    def test_clean_corpus_prints_nothing_and_exits_zero(self):
        code, out, err = run_doctor(self.cfg)
        self.assertEqual((code, out, err), (0, "", ""))

    def test_orphan_prints_its_path_and_exits_one(self):
        self.write("storefront/stray.md", "# Stray\n\nUpdated: 2026-03-02\n")
        code, out, _ = run_doctor(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("ORPHANS (1)", out)
        self.assertIn("storefront/stray.md", out)
        self.assertIn("STALE FOLDER INDEX", out)

    def test_content_loss_exits_one_and_names_the_identifier(self):
        self.remove("storefront/promo-codes.md")
        code, out, _ = run_doctor(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("CONTENT LOST", out)
        self.assertIn("SHOP-120", out)

    def test_advisories_alone_do_not_fail_the_check(self):
        """Regression: expired markers and gutted rules are review prompts, not gate failures."""
        old_line = next(l for l in self.read("standing-rules.md").splitlines() if l.startswith("3."))
        self.edit("standing-rules.md", old_line + "\n",
                  "3. **Never delete a fact while tightening a file, move it instead** short.\n")
        self.edit("storefront/promo-codes.md", "# Promo codes", "# Promo codes\n\nVerify by: 2020-01-01")
        code, out, _ = run_doctor(self.cfg)
        self.assertEqual(code, 0, out)
        self.assertIn("RULE BODIES SHRANK", out)
        self.assertIn("VERIFY BY date has passed", out)

    def test_a_corpus_without_git_says_that_loss_checking_is_off_but_still_passes(self):
        import shutil
        shutil.rmtree(self.root / ".git")
        code, out, _ = run_doctor(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("loss check skipped: the corpus root is not a git repository root", out)

    def test_missing_root_is_an_error(self):
        import shutil
        shutil.rmtree(self.root)
        code, out, err = run_doctor(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

    def test_every_heading_the_digest_counts_is_one_the_doctor_can_print(self):
        """Regression: the digest's friction counter and the doctor's wording must not drift apart."""
        self.write("storefront/stray.md", "# Stray\n")
        self.remove("storefront/promo-codes.md")
        self.write("README.md", self.read("README.md") + "- [pasted](x.md): s\n" + "  filler line of text\n" * 100)
        _, out, _ = run_doctor(self.cfg)
        for heading in ("ORPHANS", "STALE FOLDER INDEX", "MISSING DATE", "CONTENT LOST", "ROOT INDEX OVER BUDGET"):
            self.assertIn(heading, out)
            self.assertIn(heading, doctor.HEADINGS)


if __name__ == "__main__":
    unittest.main()
