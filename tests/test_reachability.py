import unittest

import support  # noqa: F401
from ctxkit.corpus import find_stale_folder_indexes, find_unreachable, mentions
from ctxkit.doctor import check_missing_date


class ReachabilityTest(support.WorkspaceCase):
    def test_fixture_corpus_is_clean(self):
        """Control: the untouched fixture has no findings, so each mutation below is what trips a check."""
        self.assertEqual(find_unreachable(self.corpus), [])
        self.assertEqual(find_stale_folder_indexes(self.corpus), [])
        self.assertEqual(check_missing_date(self.corpus), [])

    def test_three_hops_root_folder_cluster_index_is_reachable(self):
        """Regression: a file named only inside a cluster index must count as reachable (root, folder, cluster)."""
        reached = find_unreachable(self.corpus)
        self.assertNotIn("storefront/promo-codes.md", reached)
        self.assertNotIn("storefront/seasonal-campaigns.md", reached)
        # And the same chain through an archive folder.
        self.assertNotIn("fulfillment/archive/warehouse-migration.md", reached)

    def test_cluster_index_that_nobody_names_cannot_launder_reachability(self):
        """Regression: an unrouted index must not vouch for the files it lists."""
        self.edit("storefront/README.md",
                  "- [storefront-index.md](storefront-index.md): cluster index for promotions and campaigns.\n", "")
        self.assertEqual(
            find_unreachable(self.corpus),
            ["storefront/promo-codes.md", "storefront/seasonal-campaigns.md", "storefront/storefront-index.md"],
        )

    def test_a_chain_longer_than_three_reads_does_not_count(self):
        """Regression: index -> index -> file is a fourth read and must stay unreachable."""
        self.write("storefront/more-index.md", "# More\n\nUpdated: 2026-03-02\n\n- extra.md: late additions.\n")
        self.write("storefront/extra.md", "# Extra\n\nUpdated: 2026-03-02\n")
        self.edit("storefront/storefront-index.md", "- [seasonal-campaigns.md]",
                  "- [more-index.md](more-index.md): even more.\n- [seasonal-campaigns.md]")
        unreachable = find_unreachable(self.corpus)
        self.assertNotIn("storefront/more-index.md", unreachable, "the second index is itself within three reads")
        self.assertIn("storefront/extra.md", unreachable)

    def test_folder_readme_does_not_count_unless_the_root_routes_to_the_folder(self):
        """Regression: listing files in a folder README is worthless if the root never mentions the folder."""
        self.edit("README.md",
                  "- [storefront/README.md](storefront/README.md): catalogue, search, cart and promotions.\n", "")
        unreachable = find_unreachable(self.corpus)
        for name in ("cart-checkout", "search-ranking", "promo-codes", "seasonal-campaigns", "storefront-index"):
            self.assertIn(f"storefront/{name}.md", unreachable)
        self.assertNotIn("fulfillment/carrier-rates.md", unreachable)

    def test_a_root_mention_of_the_bare_file_name_is_enough(self):
        """Regression: hop one, naming the file directly in the root, must not require a folder README."""
        self.write("storefront/hotfix.md", "# Hotfix\n\nUpdated: 2026-03-02\n")
        self.assertIn("storefront/hotfix.md", find_unreachable(self.corpus))
        self.edit("README.md", "Read this first", "Read this first (urgent: hotfix.md)")
        self.assertNotIn("storefront/hotfix.md", find_unreachable(self.corpus))

    def test_a_longer_name_does_not_vouch_for_a_shorter_one(self):
        """Regression: naming api-notes.md must not make notes.md look reachable."""
        self.write("storefront/api-notes.md", "# API notes\n\nUpdated: 2026-03-02\n")
        self.write("storefront/notes.md", "# Notes\n\nUpdated: 2026-03-02\n")
        self.edit("storefront/README.md", "- [search-ranking.md]",
                  "- [api-notes.md](api-notes.md): interface notes.\n- [search-ranking.md]")
        unreachable = find_unreachable(self.corpus)
        self.assertIn("storefront/notes.md", unreachable)
        self.assertNotIn("storefront/api-notes.md", unreachable)

    def test_mentions_helper_requires_whole_names(self):
        self.assertTrue(mentions("see [a](docs/notes.md).", "notes.md"))
        self.assertTrue(mentions("see `notes.md`", "notes.md"))
        self.assertFalse(mentions("see api-notes.md", "notes.md"))
        self.assertFalse(mentions("see notes.md.bak", "notes.md"))
        self.assertTrue(mentions("route: storefront/archive/README.md", "storefront/archive"))


class StaleFolderIndexTest(support.WorkspaceCase):
    def test_files_listed_only_in_a_named_cluster_index_are_not_stale(self):
        """Regression: relocating entries into a cluster index must not report them missing from the folder README."""
        self.write("storefront/loose.md", "# Loose\n\nUpdated: 2026-03-02\n")
        self.assertEqual(find_stale_folder_indexes(self.corpus), [("storefront", ["loose.md"])])

    def test_cluster_index_that_the_folder_readme_does_not_name_does_not_clear_its_entries(self):
        """Regression: the cluster index itself and the files it lists are all missing once it is unnamed."""
        self.edit("storefront/README.md",
                  "- [storefront-index.md](storefront-index.md): cluster index for promotions and campaigns.\n", "")
        self.assertEqual(
            find_stale_folder_indexes(self.corpus),
            [("storefront", ["promo-codes.md", "seasonal-campaigns.md", "storefront-index.md"])],
        )

    def test_archive_folders_are_held_to_the_same_standard(self):
        """Regression: an archive README that forgets a file hides it from the one place agents would look."""
        self.write("fulfillment/archive/old-carrier.md", "# Old carrier\n\nUpdated: 2026-03-02\n")
        self.assertEqual(find_stale_folder_indexes(self.corpus),
                         [("fulfillment/archive", ["old-carrier.md"])])


class MissingDateTest(support.WorkspaceCase):
    def test_file_without_any_date_is_flagged(self):
        self.write("storefront/undated.md", "# Undated\n\nNothing about when this was written.\n")
        self.assertEqual(check_missing_date(self.corpus), ["storefront/undated.md"])

    def test_updated_marker_or_iso_date_in_the_head_satisfies_the_check(self):
        self.write("storefront/a.md", "# A\n\nUpdated: soon\n")
        self.write("storefront/b.md", "# B\n\nWritten 2026-01-09 after the incident.\n")
        self.assertEqual(check_missing_date(self.corpus), [])

    def test_a_date_buried_past_forty_lines_does_not_count(self):
        """Regression: the date must be visible at the top, where a reader decides whether to trust the file."""
        body = "# Late\n" + "filler line\n" * 45 + "Updated: 2026-03-02\n"
        self.write("storefront/late.md", body)
        self.assertEqual(check_missing_date(self.corpus), ["storefront/late.md"])


if __name__ == "__main__":
    unittest.main()
