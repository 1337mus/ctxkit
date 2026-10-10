import contextlib
import io
import json
import os
import sys
import time
import unittest
from datetime import date
from pathlib import Path

import support
from ctxkit import recall
from ctxkit.corpus import Corpus
from ctxkit.recall import LineIndex, build_set, content_words, judge

BASE_FACTS = {"SHOP-101", "SHOP-102", "SHOP-110", "SHOP-111", "SHOP-120", "SHOP-130", "SHOP-140", "SHOP-141", "SHOP-150"}


def capture(func, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = func(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


class RecallCase(support.WorkspaceCase):
    def configure_agent(self, timeout=30):
        command = json.dumps([sys.executable, str(support.FAKE_AGENT), "{prompt}"])
        self.load(tables=f"[recall]\nagent_command = {command}\ntimeout_seconds = {timeout}")

    def set_env(self, **values):
        for key, value in values.items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)

    def build(self, n=50, seed=1):
        return build_set(self.cfg, n, seed)

    def build_and_save(self, n=50, seed=1):
        data = self.build(n, seed)
        return recall.save_set(self.cfg, data, date(2026, 6, 1)), data


class FactSelectionTest(RecallCase):
    def test_only_topic_lines_with_exactly_one_identifier_become_facts(self):
        """Regression: indexes, archives, excluded folders, fenced code and multi-id lines must never be quizzed."""
        self.write("personal/trip.md", "# Trip\n\nUpdated: 2026-03-02\n\n- SHOP-777: pack the blue suitcase.\n")
        self.edit("storefront/README.md", "- [cart-checkout.md]", "- SHOP-888: pointer text lives in an index.\n- [cart-checkout.md]")
        self.edit("storefront/storefront-index.md", "- [promo-codes.md]", "- SHOP-889: cluster pointer.\n- [promo-codes.md]")
        self.write("storefront/fenced.md",
                   "# Fenced\n\nUpdated: 2026-03-02\n\n```\nSHOP-555: example inside a code fence\n```\n\n- SHOP-556 and SHOP-557 share one line.\n")
        answers = {i.answer for i in self.build(n=100).items}
        self.assertEqual(answers, BASE_FACTS)

    def test_a_line_repeating_one_identifier_is_still_a_single_identifier_fact(self):
        self.write("storefront/repeat.md", "# Repeat\n\nUpdated: 2026-03-02\n\n- SHOP-901 tracks the zephyr rollout, see SHOP-901 for details.\n")
        self.assertIn("SHOP-901", {i.answer for i in self.build(n=100).items})

    def test_names_on_the_ignore_list_are_not_identifiers(self):
        self.write("storefront/enc.md", "# Enc\n\nUpdated: 2026-03-02\n\n- Files are saved as UTF-8 text.\n")
        self.assertNotIn("UTF-8", {i.answer for i in self.build(n=100).items})

    def test_excluded_dirs_setting_controls_which_folders_are_skipped(self):
        self.write("personal/trip.md", "# Trip\n\nUpdated: 2026-03-02\n\n- SHOP-777: pack the blue suitcase.\n")
        self.assertNotIn("SHOP-777", {i.answer for i in self.build(n=100).items})
        self.load(top="excluded_dirs = []")
        self.assertIn("SHOP-777", {i.answer for i in self.build(n=100).items})


class DeterminismTest(RecallCase):
    def order(self, seed, n=50):
        return [(i.id, i.answer) for i in self.build(n, seed).items]

    def test_same_seed_and_corpus_give_the_same_set(self):
        """Regression: a recall trend is meaningless if the quiz changes between runs."""
        first, second = self.build(5, 7), self.build(5, 7)
        self.assertEqual([vars(i) for i in first.items], [vars(i) for i in second.items])

    def test_different_seeds_give_different_selections(self):
        orders = {tuple(a for _, a in self.order(seed)) for seed in range(1, 8)}
        self.assertGreater(len(orders), 3)

    def test_n_limits_the_set_and_a_large_n_returns_every_fact(self):
        self.assertEqual(len(self.build(3).items), 3)
        self.assertEqual(len(self.build(500).items), len(BASE_FACTS))

    def test_adding_an_unrelated_fact_does_not_reshuffle_the_existing_ones(self):
        """Regression: sampling by position instead of by hash would reorder the whole quiz on every edit."""
        before = [i.answer for i in self.build(100, 3).items]
        self.write("storefront/extra.md", "# Extra\n\nUpdated: 2026-03-02\n\n- SHOP-950: the quokka feature is enabled.\n")
        after = [i.answer for i in self.build(100, 3).items if i.answer != "SHOP-950"]
        self.assertEqual(after, before)


class CueTest(RecallCase):
    def only(self, rel, line, extra_files=0):
        """Replace the fact corpus with a single fact line so its cue can be examined alone."""
        for path in list(self.corpus.files()):
            if self.corpus.rel(path) not in ("README.md",):
                path.unlink()
        self.write(rel, f"# T\n\nUpdated: 2026-03-02\n\n{line}\n")
        return build_set(self.cfg, 10, 1).items

    def test_cue_has_no_identifier_markdown_or_stopwords_and_at_most_six_words(self):
        line = "- SHOP-501: **Bold** [kiosk lanyard](notes.md) printing is done by `manual` because of the rules."
        (item,) = self.only("storefront/a.md", line)
        self.assertLessEqual(len(item.cue), 6)
        joined = " ".join(item.cue)
        for forbidden in ("shop-501", "*", "`", "[", "]", "(", ")", "notes.md"):
            self.assertNotIn(forbidden, joined)
        self.assertEqual(set(item.cue), {"bold", "kiosk", "lanyard", "printing", "done", "manual"})

    def test_words_are_ranked_by_how_few_files_contain_them(self):
        """Regression: common words make a cue ambiguous, so the rarest must be kept first."""
        for name in ("one", "two", "three"):
            self.write(f"storefront/{name}.md", f"# {name}\n\nUpdated: 2026-03-02\n\nThe widget is ordinary here.\n")
        self.write("storefront/target.md", "# T\n\nUpdated: 2026-03-02\n\n- SHOP-601: widget calibration zebra\n")
        item = next(i for i in build_set(self.cfg, 100, 1).items if i.answer == "SHOP-601")
        self.assertEqual(item.cue, ["calibration", "zebra", "widget"])

    def test_only_the_six_rarest_words_survive(self):
        line = "- SHOP-502: alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        (item,) = self.only("storefront/a.md", line)
        self.assertEqual(len(item.cue), 6)

    def test_content_words_drop_possessives_short_words_and_duplicates(self):
        self.assertEqual(content_words("The customer's cart, the CART and an ox"), ["customer", "cart"])


class OracleTest(RecallCase):
    def test_fair_questions_are_answerable_and_record_where_the_grep_landed(self):
        items = self.build(100).items
        self.assertTrue(all(i.answerable for i in items), [(i.answer, i.reason) for i in items if not i.answerable])
        for item in items:
            self.assertIn(item.path, item.oracle_files)

    def test_a_cue_shared_with_a_line_that_has_a_different_identifier_is_ambiguous(self):
        """Regression: two correct answers make any score meaningless, so such items must be excluded."""
        self.edit("storefront/cart-checkout.md", "- Gift wrapping",
                  "- SHOP-199: signed-in customers keep cart items for 30 days while guests keep them for 3 days.\n- Gift wrapping")
        by_answer = {i.answer: i for i in self.build(100).items}
        for answer in ("SHOP-101", "SHOP-199"):
            self.assertFalse(by_answer[answer].answerable)
            self.assertEqual(by_answer[answer].reason, "ambiguous")
        self.assertTrue(by_answer["SHOP-110"].answerable)

    def test_the_same_fact_repeated_in_an_index_with_the_same_identifier_is_not_ambiguous(self):
        self.edit("storefront/README.md", "- [cart-checkout.md]",
                  "- SHOP-101: signed-in customers keep cart items for 30 days while guests keep them for 3 days.\n- [cart-checkout.md]")
        item = next(i for i in self.build(100).items if i.answer == "SHOP-101")
        self.assertTrue(item.answerable)
        self.assertEqual(sorted(item.oracle_files), ["storefront/README.md", "storefront/cart-checkout.md"])

    def test_a_line_with_nothing_distinctive_has_no_cue(self):
        self.write("storefront/vague.md", "# V\n\nUpdated: 2026-03-02\n\n- SHOP-700: it is all of the and.\n")
        item = next(i for i in self.build(100).items if i.answer == "SHOP-700")
        self.assertEqual((item.answerable, item.reason, item.cue), (False, "no_cue", []))

    def test_grep_lands_only_on_lines_where_every_word_appears_together(self):
        """Regression: AND must mean the same line. File-level AND would call unrelated pages a match."""
        index = LineIndex(Corpus(self.cfg))
        self.assertTrue(index.and_grep(["recency"])[0])
        self.assertTrue(index.and_grep(["linearly"])[0])
        self.assertEqual(index.and_grep(["recency", "carrier"]), (set(), set()), "different lines, different files")
        files, ids = index.and_grep(["recency", "linearly"])
        self.assertEqual((files, ids), ({"storefront/search-ranking.md"}, {"SHOP-110"}))

    def test_a_cue_that_misses_its_source_file_is_an_oracle_miss(self):
        corpus = Corpus(self.cfg)
        fact = next(f for f in recall.iter_facts(corpus) if f.identifier == "SHOP-110")
        answerable, reason, files = judge(["carrier", "freight"], fact, LineIndex(corpus))
        self.assertEqual((answerable, reason), (False, "oracle_miss"))
        self.assertEqual(files, ["fulfillment/carrier-rates.md"])

    def test_unanswerable_items_stay_in_the_saved_set_but_are_flagged(self):
        self.write("storefront/vague.md", "# V\n\nUpdated: 2026-03-02\n\n- SHOP-700: it is all of the and.\n")
        path, _ = self.build_and_save(100)
        saved = json.loads(path.read_text())
        self.assertEqual(path, self.state / "recall" / "set-2026-06-01.json")
        flagged = [i for i in saved["items"] if not i["answerable"]]
        self.assertEqual([i["answer"] for i in flagged], ["SHOP-700"])
        self.assertEqual({"version", "created", "root", "seed", "n_requested", "facts_available", "items"}, set(saved))

    def test_build_command_reports_counts_and_reasons(self):
        self.write("storefront/vague.md", "# V\n\nUpdated: 2026-03-02\n\n- SHOP-700: it is all of the and.\n")
        code, out, _ = capture(recall.cmd_build, self.cfg, 100, 1, None, date(2026, 6, 1))
        self.assertEqual(code, 0)
        self.assertIn("10 item(s) from 10 fact line(s)", out)
        self.assertIn("answerable: 9", out)
        self.assertIn("unanswerable (no_cue): 1", out)


class RunTest(RecallCase):
    def setUp(self):
        super().setUp()
        self.configure_agent()

    def summary(self):
        (path,) = (self.state / "recall").glob("run-*.summary.json")
        return json.loads(path.read_text())

    def records(self):
        (path,) = (self.state / "recall").glob("run-*.jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_a_competent_agent_scores_every_answerable_item(self):
        """Regression: the whole pipeline (cue, prompt, agent, parsing, scoring) must agree end to end."""
        self.build_and_save()
        code, out, err = capture(recall.cmd_run, self.cfg)
        self.assertEqual((code, err), (0, ""))
        s = self.summary()
        self.assertEqual((s["n_items"], s["n_answerable"], s["n_run"], s["n_correct"]), (9, 9, 9, 9))
        self.assertEqual(s["accuracy"], 1.0)
        self.assertEqual(s["answerable_rate"], 1.0)
        self.assertGreater(s["median_seconds"], 0)
        self.assertIsNone(s["median_tokens"], "plain text output carries no usage")
        records = self.records()
        self.assertEqual(len(records), 9)
        self.assertTrue(all(r["correct"] and r["predicted"] == r["answer"] for r in records))
        self.assertIn("accuracy: 9/9 (100.0%)", out)

    def test_unanswerable_items_are_neither_run_nor_scored(self):
        self.edit("storefront/cart-checkout.md", "- Gift wrapping",
                  "- SHOP-199: signed-in customers keep cart items for 30 days while guests keep them for 3 days.\n- Gift wrapping")
        self.build_and_save()
        capture(recall.cmd_run, self.cfg)
        s = self.summary()
        self.assertEqual((s["n_items"], s["n_answerable"], s["n_run"]), (10, 8, 8))
        self.assertAlmostEqual(s["answerable_rate"], 8 / 10)
        self.assertEqual(len(self.records()), 8)
        self.assertEqual(s["accuracy"], 1.0)

    def test_a_wrong_or_abstaining_agent_is_scored_down(self):
        _, data = self.build_and_save()
        victim = data.items[0]
        unique_word = victim.cue[0]
        self.set_env(FAKE_AGENT_SABOTAGE=unique_word)
        capture(recall.cmd_run, self.cfg)
        s = self.summary()
        self.assertEqual((s["n_correct"], s["n_abstained"]), (8, 1))
        self.assertAlmostEqual(s["accuracy"], 8 / 9)
        missed = [r for r in self.records() if not r["correct"]]
        self.assertEqual([(r["item"], r["abstained"]) for r in missed], [(victim.id, True)])

    def test_confidently_wrong_answers_are_misses_not_abstentions(self):
        self.build_and_save()
        self.set_env(FAKE_AGENT_MODE="wrong")
        capture(recall.cmd_run, self.cfg)
        s = self.summary()
        self.assertEqual((s["n_correct"], s["n_abstained"], s["accuracy"]), (0, 0, 0.0))

    def test_markdown_decoration_on_the_last_line_does_not_defeat_scoring(self):
        self.build_and_save()
        self.set_env(FAKE_AGENT_MODE="formatted")
        capture(recall.cmd_run, self.cfg)
        self.assertEqual(self.summary()["accuracy"], 1.0)

    def test_claude_json_usage_is_summed_into_tokens(self):
        self.build_and_save()
        self.set_env(FAKE_AGENT_MODE="claude_json")
        capture(recall.cmd_run, self.cfg)
        self.assertEqual(self.summary()["accuracy"], 1.0)
        self.assertEqual(self.summary()["median_tokens"], 150)
        self.assertTrue(all(r["tokens"] == 150 for r in self.records()))

    def test_codex_style_tokens_used_line_is_parsed_from_stderr(self):
        self.build_and_save()
        self.set_env(FAKE_AGENT_MODE="codex_text")
        capture(recall.cmd_run, self.cfg)
        self.assertEqual((self.summary()["accuracy"], self.summary()["median_tokens"]), (1.0, 1234))

    def test_a_hung_agent_is_cut_off_at_the_timeout_and_recorded(self):
        """Regression: one stuck agent process must not hang the whole evaluation."""
        self.configure_agent(timeout=1)
        self.build_and_save()
        self.set_env(FAKE_AGENT_MODE="sleep")
        started = time.monotonic()
        code, _, _ = capture(recall.cmd_run, self.cfg, None, 1)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 15)
        s = self.summary()
        self.assertEqual((s["n_run"], s["n_timeouts"], s["accuracy"]), (1, 1, 0.0))
        self.assertTrue(self.records()[0]["timed_out"])

    def test_a_failing_agent_is_counted_as_errors_with_the_exit_status(self):
        self.build_and_save()
        self.set_env(FAKE_AGENT_MODE="fail")
        capture(recall.cmd_run, self.cfg)
        s = self.summary()
        self.assertEqual((s["n_errors"], s["n_correct"]), (9, 0))
        self.assertIn("exit 3", self.records()[0]["error"])

    def test_limit_runs_only_the_first_items(self):
        self.build_and_save()
        capture(recall.cmd_run, self.cfg, None, 2)
        self.assertEqual((self.summary()["n_run"], self.summary()["n_answerable"]), (2, 9))

    def test_the_prompt_is_read_only_starts_at_the_readme_and_runs_in_the_corpus(self):
        """Regression: the instructions are the experiment. They must tell the agent where to start and not to edit."""
        dump = self.tmp / "prompt.json"
        self.set_env(FAKE_AGENT_DUMP=str(dump))
        self.build_and_save()
        capture(recall.cmd_run, self.cfg, None, 1)
        seen = json.loads(dump.read_text())
        prompt = seen["prompt"]
        for needle in (f"read-only markdown knowledge base at {self.root}", "README.md", "Never edit",
                       "identifier", "last line", "NONE", "Clues: "):
            self.assertIn(needle, prompt)
        self.assertEqual(Path(seen["cwd"]).resolve(), self.root.resolve())

    def test_corpus_changes_during_a_run_are_flagged(self):
        """Regression: a write-capable agent would corrupt the knowledge base, so the run must say so loudly."""
        self.build_and_save()
        self.set_env(FAKE_AGENT_MODE="write")
        code, _, err = capture(recall.cmd_run, self.cfg, None, 1)
        self.assertEqual(code, 0)
        self.assertIn("WARNING: the corpus changed", err)
        self.assertTrue(self.summary()["corpus_modified_during_run"])

    def test_an_untouched_corpus_is_not_flagged(self):
        self.build_and_save()
        capture(recall.cmd_run, self.cfg, None, 1)
        self.assertFalse(self.summary()["corpus_modified_during_run"])

    def test_default_set_is_the_newest_and_set_can_be_chosen(self):
        old, _ = self.build_and_save(n=3, seed=1)
        old.rename(old.with_name("set-2026-01-01.json"))
        new, _ = self.build_and_save(n=5, seed=2)
        capture(recall.cmd_run, self.cfg)
        self.assertEqual(self.summary()["set"], new.name)
        for p in (self.state / "recall").glob("run-*"):
            p.unlink()
        capture(recall.cmd_run, self.cfg, self.state / "recall" / "set-2026-01-01.json")
        self.assertEqual((self.summary()["set"], self.summary()["n_run"]), ("set-2026-01-01.json", 3))


class RunPreconditionsTest(RecallCase):
    def test_missing_prompt_placeholder_is_a_usage_error(self):
        self.load(tables='[recall]\nagent_command = ["claude", "-p"]')
        self.build_and_save()
        code, _, err = capture(recall.cmd_run, self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("{prompt}", err)

    def test_unconfigured_agent_is_a_usage_error(self):
        self.build_and_save()
        self.assertEqual(capture(recall.cmd_run, self.cfg)[0], 2)

    def test_no_set_is_a_usage_error_that_names_the_fix(self):
        self.configure_agent()
        code, _, err = capture(recall.cmd_run, self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("ctx recall build", err)

    def test_a_missing_agent_binary_aborts_once_without_writing_a_run(self):
        command = json.dumps([str(self.tmp / "no-such-agent"), "{prompt}"])
        self.load(tables=f"[recall]\nagent_command = {command}")
        self.build_and_save()
        code, _, err = capture(recall.cmd_run, self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("cannot start the agent command", err)
        self.assertEqual(list((self.state / "recall").glob("run-*")), [])

    def test_a_corrupt_set_file_is_a_clear_error(self):
        self.configure_agent()
        path = self.state / "recall" / "set-2026-06-01.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        code, _, err = capture(recall.cmd_run, self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("cannot read recall set", err)


class ReportTest(RecallCase):
    def write_summary(self, name, started, set_name, accuracy, n_run=10):
        directory = self.state / "recall"
        directory.mkdir(parents=True, exist_ok=True)
        data = {"run_id": name, "started": started, "set": set_name, "accuracy": accuracy, "n_correct": round(accuracy * n_run),
                "n_run": n_run, "answerable_rate": 0.9, "median_seconds": 12.5, "median_tokens": 18000}
        (directory / f"run-{name}.summary.json").write_text(json.dumps(data))

    def test_no_runs_says_how_to_start(self):
        code, out, _ = capture(recall.cmd_report, self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("no recall runs yet", out)

    def test_trend_lists_runs_in_order_and_only_compares_runs_on_the_same_set(self):
        """Regression: accuracy on a different quiz is not a trend, so the change column must say 'new set'."""
        self.write_summary("a", "2026-05-01T09:00:00Z", "set-1.json", 0.6)
        self.write_summary("b", "2026-05-08T09:00:00Z", "set-1.json", 0.8)
        self.write_summary("c", "2026-05-15T09:00:00Z", "set-2.json", 0.5)
        (self.state / "recall" / "run-bad.summary.json").write_text("{nope")
        code, out, _ = capture(recall.cmd_report, self.cfg)
        self.assertEqual(code, 0)
        rows = [l for l in out.splitlines() if l.startswith("2026-")]
        self.assertEqual(len(rows), 3)
        self.assertIn("new set", rows[0])
        self.assertIn("+20.0", rows[1])
        self.assertIn("new set", rows[2])
        self.assertIn("RECALL TREND (3 run(s))", out)
        self.assertIn("18000", rows[0])

    def test_report_follows_a_real_run(self):
        self.configure_agent()
        self.build_and_save()
        capture(recall.cmd_run, self.cfg)
        _, out, _ = capture(recall.cmd_report, self.cfg)
        self.assertIn("RECALL TREND (1 run(s))", out)
        self.assertIn("9/9", out)
        self.assertIn("100.0%", out)


if __name__ == "__main__":
    unittest.main()
