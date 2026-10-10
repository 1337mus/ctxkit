import contextlib
import io
import shutil
import time
import unittest
from datetime import date

import builders
from ctxkit import stats
from ctxkit.stats import compute_stats, read_trend


def run_stats(cfg, days=30, now=None):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = stats.run(cfg, days, now)
    return code, out.getvalue(), err.getvalue()


class StatsCase(builders.TranscriptCase):
    def setUp(self):
        super().setUp()
        for path in self.corpus.files():
            self.set_age(self.corpus.rel(path), 30)

    def claude(self, name, turns, reads=(), writes=()):
        lines = [builders.claude_tool("Read", file_path=self.root_path(r)) for r in reads]
        lines += [builders.claude_tool("Edit", file_path=self.root_path(w), old_string="a", new_string="b") for w in writes]
        lines += [builders.claude_text() for _ in range(max(0, turns - len(lines)))]
        return self.add_claude(name, lines)

    def codex(self, name, turns, reads=(), field="arguments", patched=()):
        lines = []
        for r in reads:
            if field == "arguments":
                lines.append(builders.codex_call(f"cat {self.root_path(r)}", field="arguments"))
            else:
                lines.append(builders.codex_call(f"cat {self.root_path(r)}", field="input", name="shell", kind="custom_tool_call"))
        lines += [builders.codex_patch_end(self.root_path(p)) for p in patched]
        lines += [builders.codex_message() for _ in range(turns)]
        return self.add_codex(name, lines)


class ComplianceTest(StatsCase):
    def build_mixed(self):
        self.claude("c-reads", 22, reads=["README.md", "storefront/README.md"])
        self.claude("c-none", 25)
        self.claude("c-short", 5, reads=["storefront/cart-checkout.md"])
        self.codex("x-reads", 24, reads=["README.md", "fulfillment/carrier-rates.md"])
        self.codex("x-none", 30)
        self.codex("x-old-field", 21, reads=["fulfillment/returns-policy.md"], field="input")
        self.codex("x-index-only", 26, reads=["README.md"])

    def test_compliance_is_reported_per_tool_and_combined(self):
        """Regression: Codex sessions must be scored on the same footing as Claude sessions."""
        self.build_mixed()
        r = compute_stats(self.cfg, 30)
        self.assertEqual((r.per_tool["claude"].substantial, r.per_tool["claude"].compliant), (2, 1))
        self.assertEqual((r.per_tool["codex"].substantial, r.per_tool["codex"].compliant), (4, 3))
        self.assertEqual((r.combined.substantial, r.combined.compliant), (6, 4))
        self.assertAlmostEqual(r.combined.pct, 66.7, places=1)
        self.assertEqual(r.combined.sessions, 7)

    def test_the_substantial_threshold_is_inclusive_and_configurable(self):
        self.claude("exact", 20, reads=["README.md"])
        self.claude("under", 19, reads=["README.md"])
        self.assertEqual(compute_stats(self.cfg, 30).combined.substantial, 1)
        self.load(top="substantial_min_turns = 5")
        self.assertEqual(compute_stats(self.cfg, 30).combined.substantial, 2)

    def test_a_disabled_source_is_left_out_of_every_number(self):
        self.build_mixed()
        self.load(codex_extra="enabled = false")
        r = compute_stats(self.cfg, 30)
        self.assertEqual((r.combined.substantial, r.per_tool["codex"].sessions), (2, 0))

    def test_sessions_outside_the_window_are_ignored(self):
        self.add_claude("old", [builders.claude_text()] * 30, age_days=45)
        self.assertEqual(compute_stats(self.cfg, 30).combined.sessions, 0)
        self.assertEqual(compute_stats(self.cfg, 60).combined.sessions, 1)

    def test_corpus_writes_are_counted_separately_from_reads(self):
        self.claude("writer", 22, writes=["storefront/promo-codes.md"])
        self.codex("patcher", 22, patched=["storefront/search-ranking.md"])
        self.claude("reader-only", 22, reads=["README.md"])
        r = compute_stats(self.cfg, 30)
        self.assertEqual(r.combined.wrote, 2)
        self.assertEqual(r.combined.compliant, 1, "writing a file is not pulling from the corpus")

    def test_top_read_counts_distinct_sessions_across_both_tools(self):
        self.build_mixed()
        top = dict(compute_stats(self.cfg, 30).top_read)
        self.assertEqual(top["README.md"], 3)
        self.assertEqual(top["fulfillment/carrier-rates.md"], 1)
        self.assertNotIn("storefront/cart-checkout.md", top, "short sessions do not count toward top read")


class DeadWeightTest(StatsCase):
    def test_a_file_read_only_by_codex_is_not_dead_weight(self):
        """Regression: dead weight once counted Claude reads only, so Codex-read files were reported as unused."""
        self.codex("only-codex", 22, reads=["fulfillment/carrier-rates.md", "storefront/search-ranking.md"])
        dead = compute_stats(self.cfg, 30).dead
        self.assertNotIn("fulfillment/carrier-rates.md", dead)
        self.assertNotIn("storefront/search-ranking.md", dead)
        self.assertIn("storefront/promo-codes.md", dead)

    def test_reads_from_both_tools_and_short_sessions_all_count(self):
        self.claude("c", 5, reads=["storefront/cart-checkout.md"])
        self.codex("x", 3, reads=["fulfillment/returns-policy.md"], field="input")
        dead = compute_stats(self.cfg, 30).dead
        self.assertEqual(dead, ["fulfillment/carrier-rates.md", "standing-rules.md", "storefront/promo-codes.md",
                                "storefront/search-ranking.md",
                                "storefront/seasonal-campaigns.md", "storefront/storefront-index.md"])
        self.assertEqual([d for d in dead if d.endswith("README.md")], [])

    def test_young_files_archive_and_excluded_folders_are_never_dead_weight(self):
        self.write("personal/travel.md", "# Travel\n\nUpdated: 2026-03-02\n")
        self.set_age("personal/travel.md", 60)
        self.set_age("storefront/promo-codes.md", 1)
        dead = compute_stats(self.cfg, 30).dead
        self.assertNotIn("personal/travel.md", dead)
        self.assertNotIn("storefront/promo-codes.md", dead, "a file written yesterday has had no fair chance to be read")
        self.assertFalse([d for d in dead if "archive/" in d])
        self.load(top='excluded_dirs = []')
        self.assertIn("personal/travel.md", compute_stats(self.cfg, 30).dead)


class IndexRoutingTest(StatsCase):
    def test_routing_counts_both_tools(self):
        """Regression: routing was measured on Claude sessions only and fell apart once usage moved to Codex."""
        self.claude("c-routed", 22, reads=["README.md", "storefront/README.md"])
        self.codex("x-routed", 22, reads=["README.md", "fulfillment/carrier-rates.md"])
        self.codex("x-stuck", 22, reads=["README.md"])
        self.claude("c-never-opened-index", 22, reads=["storefront/cart-checkout.md"])
        ro = compute_stats(self.cfg, 30).routing
        self.assertEqual((ro.read_index, ro.routed, ro.failed), (3, 2, 1))
        self.assertAlmostEqual(ro.success_pct, 66.7, places=1)
        self.assertAlmostEqual(ro.failure_pct, 33.3, places=1)

    def test_index_size_is_flagged_past_the_token_limit(self):
        self.assertFalse(compute_stats(self.cfg, 30).index_flagged)
        self.load(top="index_token_flag = 50")
        r = compute_stats(self.cfg, 30)
        self.assertTrue(r.index_flagged)
        self.assertEqual(r.index_tokens, r.index_bytes // 4)


class StatsCommandTest(StatsCase):
    def test_output_lists_both_tools_and_the_index_numbers(self):
        self.claude("c", 22, reads=["README.md", "storefront/README.md"])
        self.codex("x", 22, reads=["README.md"])
        code, out, _ = run_stats(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("PULL COMPLIANCE (combined): 2/2", out)
        self.assertIn("claude: 1/1", out)
        self.assertIn("codex:  1/1", out)
        self.assertIn("INDEX SIZE: README.md is", out)
        self.assertIn("INDEX ROUTING: 1/2", out)

    def test_one_trend_row_per_day_and_only_for_the_canonical_window(self):
        """Regression: ad hoc runs and repeat runs must not distort the daily series."""
        self.claude("c", 22, reads=["README.md"])
        now = time.time()
        run_stats(self.cfg, 30, now)
        run_stats(self.cfg, 30, now)
        self.assertEqual([r.date for r in read_trend(self.cfg)], [date.fromtimestamp(now).isoformat()])
        code, out, _ = run_stats(self.cfg, 7, now)
        self.assertIn("not recorded to the trend", out)
        self.assertEqual(len(read_trend(self.cfg)), 1)
        run_stats(self.cfg, 30, now + 86400)
        rows = read_trend(self.cfg)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].date, rows[1].date)

    def test_trend_line_compares_with_the_previous_day_not_with_itself(self):
        self.claude("c", 22, reads=["README.md"])
        now = time.time()
        _, first, _ = run_stats(self.cfg, 30, now)
        self.assertIn("Trend: no prior record", first)
        self.claude("c2", 22)
        _, second, _ = run_stats(self.cfg, 30, now + 86400)
        self.assertIn("-> now 50.0% (-50.0)", second)
        self.assertIn("previous", second)

    def test_zero_transcripts_is_called_out_with_the_globs_that_were_searched(self):
        """Regression: a wrong glob looks exactly like unused notes unless the report says nothing was found."""
        _, out, _ = run_stats(self.cfg)
        self.assertIn("NO TRANSCRIPTS FOUND", out)
        self.assertIn(f"claude: {self.cfg.claude_glob}", out)
        self.assertIn(f"codex: {self.cfg.codex_glob}", out)
        self.claude("c", 22, reads=["README.md"])
        _, out, _ = run_stats(self.cfg)
        self.assertNotIn("NO TRANSCRIPTS FOUND", out)

    def test_trend_row_layout_keeps_the_first_three_fields_stable(self):
        self.claude("c", 22, reads=["README.md"])
        self.codex("x", 22)
        run_stats(self.cfg, 30)
        lines = (self.state / "compliance.tsv").read_text().splitlines()
        self.assertEqual(lines[0], stats.TREND_HEADER)
        fields = lines[1].split("\t")
        self.assertEqual(fields[1:3], ["2", "50.0"])
        self.assertEqual(fields[3:], ["1", "100.0", "1", "0.0"])

    def test_trend_reader_tolerates_headers_and_junk_rows(self):
        self.state.mkdir(parents=True)
        (self.state / "compliance.tsv").write_text(
            "date\tsubstantial\tpct\n2026-03-01\t10\t70.0\njunk line\n2026-03-02\tx\t1\n2026-03-03\t12\t75.0\t8\t80.0\t4\t66.7\n")
        self.assertEqual([(r.date, r.pct) for r in read_trend(self.cfg)], [("2026-03-01", 70.0), ("2026-03-03", 75.0)])

    def test_missing_corpus_root_is_an_error_and_records_nothing(self):
        shutil.rmtree(self.root)
        code, _, err = run_stats(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertFalse((self.state / "compliance.tsv").exists())


if __name__ == "__main__":
    unittest.main()
