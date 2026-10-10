import contextlib
import io
import shutil
import unittest

import support
from ctxkit import snapshot


def run_snapshot(cfg):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = snapshot.run(cfg)
    return code, out.getvalue(), err.getvalue()


class SnapshotTest(support.WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.init_repo()

    def status(self):
        return support.git(self.root, "status", "--porcelain").strip()

    def test_dirty_corpus_is_committed_with_a_neutral_message(self):
        """Regression: edits must reach history without any agent remembering to commit."""
        self.edit("storefront/promo-codes.md", "never stack", "do not stack")
        self.write("storefront/new-note.md", "# New\n\nUpdated: 2026-03-02\n")
        before = self.commit_count()
        code, out, _ = run_snapshot(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(self.commit_count(), before + 1)
        self.assertEqual(self.head_message(), "snapshot: 2 file(s) changed")
        self.assertEqual(self.status(), "", "everything, including new files, is committed")
        self.assertIn("committed 2 file(s)", out)

    def test_clean_corpus_makes_no_commit_and_prints_nothing(self):
        before = self.commit_count()
        self.assertEqual(run_snapshot(self.cfg), (0, "", ""))
        self.assertEqual(self.commit_count(), before)

    def test_refuses_to_commit_while_an_identifier_is_lost(self):
        """Regression: committing a loss would make it the new baseline and silence the detector."""
        self.remove("storefront/promo-codes.md")
        before = self.commit_count()
        code, out, _ = run_snapshot(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)
        self.assertIn("SHOP-120", out)
        self.assertEqual(self.commit_count(), before)
        self.assertNotEqual(self.status(), "", "the working tree is left exactly as it was")

    def test_a_deliberate_removal_committed_by_hand_unblocks_later_snapshots(self):
        self.remove("storefront/promo-codes.md")
        self.commit_all("retire promo codes on purpose")
        self.edit("storefront/search-ranking.md", "linearly", "gradually")
        code, _, _ = run_snapshot(self.cfg)
        self.assertEqual(code, 0)
        self.assertEqual(self.head_message(), "snapshot: 1 file(s) changed")

    def test_unsigned_by_default_even_when_the_repo_asks_for_signing(self):
        """Regression: an unattended job cannot answer a passphrase prompt, so it must never try to sign."""
        support.git(self.root, "config", "commit.gpgsign", "true")
        support.git(self.root, "config", "gpg.program", "/nonexistent/gpg")
        self.edit("storefront/promo-codes.md", "never stack", "do not stack")
        code, _, err = run_snapshot(self.cfg)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.head_message(), "snapshot: 1 file(s) changed")

    def test_sign_true_asks_git_to_sign(self):
        self.load(tables="[snapshot]\nsign = true")
        support.git(self.root, "config", "gpg.program", "/nonexistent/gpg")
        self.edit("storefront/promo-codes.md", "never stack", "do not stack")
        before = self.commit_count()
        code, _, err = run_snapshot(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("commit failed", err)
        self.assertEqual(self.commit_count(), before)

    def test_commits_with_a_fallback_identity_when_git_has_none(self):
        """Regression: a scheduler with a bare environment has no git identity and must still commit."""
        support.git(self.root, "config", "--unset", "user.name")
        support.git(self.root, "config", "--unset", "user.email")
        self.edit("storefront/promo-codes.md", "never stack", "do not stack")
        code, _, err = run_snapshot(self.cfg)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(support.git(self.root, "log", "-1", "--format=%an").strip(), "ctxkit")

    def test_disabled_in_config_does_nothing(self):
        self.load(tables="[snapshot]\nenabled = false")
        self.edit("storefront/promo-codes.md", "never stack", "do not stack")
        before = self.commit_count()
        code, out, _ = run_snapshot(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("disabled", out)
        self.assertEqual(self.commit_count(), before)


class SnapshotEdgeCasesTest(support.WorkspaceCase):
    def test_not_a_git_repo_is_a_no_op(self):
        code, out, _ = run_snapshot(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("not a git repo", out)

    def test_first_snapshot_of_a_repo_with_no_commits_commits_everything(self):
        """Regression: the loss check has no baseline yet, which must not block creating the first one."""
        support.git(self.root, "init", "-q")
        support.git(self.root, "config", "user.name", "ctxkit-tests")
        support.git(self.root, "config", "user.email", "tests@localhost")
        code, out, _ = run_snapshot(self.cfg)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commit_count(), 1)

    def test_missing_root_is_an_error(self):
        shutil.rmtree(self.root)
        code, _, err = run_snapshot(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)


if __name__ == "__main__":
    unittest.main()
