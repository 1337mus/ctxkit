import unittest
from unittest import mock

import support
from ctxkit import doctor
from ctxkit.doctor import check_gutted, check_loss

RULE_3_SHORT = "3. **Never delete a fact while tightening a file, move it instead** because losses hide there.\n"


class LossTest(support.WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.init_repo()

    def test_untouched_corpus_reports_nothing(self):
        """Control: a clean checkout is silent, so the cases below are triggered by their edits."""
        report = check_loss(self.corpus)
        self.assertEqual((report.ids, report.rules, report.skipped), ([], [], ""))

    def test_identifier_removed_from_the_whole_corpus_is_flagged(self):
        """Regression: a condense pass that drops a ticket key must be caught before it is committed."""
        self.edit("storefront/search-ranking.md",
                  "- SHOP-110: new releases get a recency boost that decays linearly over 45 days.\n",
                  "- New releases get a recency boost.\n")
        report = check_loss(self.corpus)
        self.assertEqual(report.ids, ["SHOP-110"])
        self.assertEqual(report.lost, ["SHOP-110"])

    def test_identifier_moved_to_another_file_is_not_a_loss(self):
        """Regression: relocating a line between files is normal editing and must not flag."""
        line = "- SHOP-110: new releases get a recency boost that decays linearly over 45 days.\n"
        self.edit("storefront/search-ranking.md", line, "")
        self.edit("storefront/promo-codes.md", "- SHOP-120", line + "- SHOP-120")
        self.assertEqual(check_loss(self.corpus).lost, [])

    def test_whole_file_rename_is_not_a_loss(self):
        """Regression: git mv of a file keeps every identifier alive somewhere."""
        support.git(self.root, "mv", "storefront/search-ranking.md", "storefront/ranking-signals.md")
        self.write("storefront/README.md", self.read("storefront/README.md").replace("search-ranking.md", "ranking-signals.md"))
        self.assertEqual(check_loss(self.corpus).lost, [])

    def test_identifier_that_survives_only_in_an_untracked_new_file_is_not_a_loss(self):
        """Regression: content that moved into a brand new file has not left the corpus."""
        self.edit("storefront/search-ranking.md", "- SHOP-111: the author-name boost was removed because it buried exact title matches.\n", "")
        self.assertEqual(check_loss(self.corpus).ids, ["SHOP-111"])
        self.write("storefront/ranking-history.md", "# History\n\nUpdated: 2026-03-02\n\n- SHOP-111: author boost removed.\n")
        self.assertEqual(check_loss(self.corpus).lost, [])

    def test_deleting_a_file_that_held_the_only_mention_is_flagged(self):
        self.remove("storefront/promo-codes.md")
        self.assertEqual(check_loss(self.corpus).ids, ["SHOP-120"])

    def test_committing_the_removal_clears_the_finding(self):
        """Regression: the documented escape hatch, commit a deliberate removal, must work."""
        self.remove("storefront/promo-codes.md")
        self.assertEqual(check_loss(self.corpus).ids, ["SHOP-120"])
        self.commit_all("drop promo codes: the feature was retired")
        self.assertEqual(check_loss(self.corpus).lost, [])

    def test_a_removed_line_that_starts_with_dashes_is_still_read_as_removed(self):
        """Regression: diff headers start with three dashes, so a removed line like '-- text' looks like one."""
        self.edit("storefront/cart-checkout.md", "Gift wrapping",
                  "-- SHOP-300 is the legacy marker.\nGift wrapping")
        self.commit_all("add legacy marker")
        self.edit("storefront/cart-checkout.md", "-- SHOP-300 is the legacy marker.\n", "")
        self.assertEqual(check_loss(self.corpus).ids, ["SHOP-300"])

    def test_an_identifier_that_only_appears_in_a_file_name_is_not_content(self):
        """Regression: diff headers name the file, so reading them as removed lines reports a loss that never happened."""
        self.write("storefront/SHOP-321-notes.md", "# Notes\n\nUpdated: 2026-03-02\n\n- plain text, no key here.\n")
        self.commit_all("add a note named after a ticket")
        self.edit("storefront/SHOP-321-notes.md", "plain text", "edited text")
        self.assertEqual(check_loss(self.corpus).lost, [])

    def test_perl_style_patterns_work_in_the_loss_check(self):
        """Regression: patterns using \\d or (?:...) must not be silently rejected by a POSIX-only tool."""
        self.load(top='identifier_patterns = ["(?:SHOP|OPS)-\\\\d+"]')
        self.edit("storefront/promo-codes.md", "SHOP-120", "OPS-9")
        self.commit_all("introduce an ops key")
        self.edit("storefront/promo-codes.md", "OPS-9", "nothing")
        self.assertEqual(check_loss(self.corpus).ids, ["OPS-9"])

    def test_names_on_the_ignore_list_are_never_reported(self):
        self.edit("storefront/promo-codes.md", "# Promo codes", "# Promo codes\n\nEncoding is UTF-8.")
        self.commit_all("note encoding")
        self.edit("storefront/promo-codes.md", "\n\nEncoding is UTF-8.", "")
        self.assertEqual(check_loss(self.corpus).lost, [])

    def test_missing_rule_lead_is_flagged_even_though_it_carries_no_identifier(self):
        """Regression: prose rules have no ticket key, so the bold lead is the only thing to key on."""
        text = self.read("standing-rules.md")
        kept = "".join(line for line in text.splitlines(keepends=True) if "Name the ticket key" not in line)
        self.write("standing-rules.md", kept)
        report = check_loss(self.corpus)
        self.assertEqual(report.ids, [])
        self.assertEqual(len(report.rules), 1)
        self.assertTrue(report.rules[0].startswith("Name the ticket key beside every decision"))
        self.assertTrue(report.lost[0].startswith('rule "Name the ticket key'))

    def test_reflowed_rule_with_the_same_lead_is_not_a_loss(self):
        """Regression: rewording a rule body while keeping its lead is editing, not loss."""
        self.edit("standing-rules.md", "and keep the key when you rewrite the line.", "and carry it across rewrites.")
        self.assertEqual(check_loss(self.corpus).lost, [])

    def test_a_missing_repo_or_baseline_is_stated_not_silently_treated_as_clean(self):
        """Regression: with no git history the check cannot work, and saying nothing looks like a clean bill of health."""
        import shutil
        shutil.rmtree(self.root / ".git")
        report = check_loss(self.corpus)
        self.assertEqual(report.lost, [])
        self.assertIn("not a git repository", report.skipped)
        support.git(self.root, "init", "-q")
        report = check_loss(self.corpus)
        self.assertEqual((report.lost, report.skipped), ([], "no commits yet"))


class GuttedRuleTest(support.WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.init_repo()

    def test_unchanged_corpus_reports_nothing(self):
        self.assertEqual(check_gutted(self.corpus), [])

    def test_long_rule_cut_to_a_stub_under_the_same_lead_is_reported_with_sizes(self):
        """Regression: a rule can lose most of its body while its bold lead keeps it looking intact."""
        old_line = next(l for l in self.read("standing-rules.md").splitlines() if l.startswith("3."))
        self.edit("standing-rules.md", old_line + "\n", RULE_3_SHORT)
        findings = check_gutted(self.corpus)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.path, "standing-rules.md")
        self.assertEqual(finding.lead, "Never delete a fact while tightening a file, move it instead")
        self.assertEqual(finding.old_len, len(old_line))
        self.assertEqual(finding.new_len, len(RULE_3_SHORT.rstrip("\n")))
        self.assertGreater(finding.old_len, 300)

    def test_modest_tightening_and_short_rules_are_not_reported(self):
        """Regression: only sharp shrinkage of a substantial rule is worth a human's time."""
        old_line = next(l for l in self.read("standing-rules.md").splitlines() if l.startswith("3."))
        self.edit("standing-rules.md", old_line + "\n", old_line[: int(len(old_line) * 0.7)] + "\n")
        self.assertEqual(check_gutted(self.corpus), [], "a 30 percent trim is ordinary tightening")
        support.git(self.root, "checkout", "--", "standing-rules.md")
        self.edit("standing-rules.md", "because most answers are one hop from README.md and a blind search wastes the budget.", "because.")
        self.assertEqual(check_gutted(self.corpus), [], "a rule under 300 bytes is too small to call gutted")

    def test_only_files_git_reports_as_changed_are_inspected(self):
        """Regression: the check must not spawn one git process per file in the corpus on every run."""
        old_line = next(l for l in self.read("standing-rules.md").splitlines() if l.startswith("3."))
        self.edit("standing-rules.md", old_line + "\n", RULE_3_SHORT)
        calls = []
        real = doctor.run_git

        def spy(root, *args, **kwargs):
            calls.append(args)
            return real(root, *args, **kwargs)

        with mock.patch.object(doctor, "run_git", side_effect=spy):
            check_gutted(self.corpus)
        shows = [a for a in calls if a and a[0] == "show"]
        self.assertEqual(len(shows), 1)
        self.assertGreater(len(self.corpus.files()), 5)


if __name__ == "__main__":
    unittest.main()
