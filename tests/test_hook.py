import json
import os
import unittest

import builders
from ctxkit import hook
from ctxkit.hook import milestone_count


class HookCase(builders.TranscriptCase):
    def setUp(self):
        super().setUp()
        self.session = "sess-1"
        self.transcript = self.add_claude(self.session, [])

    def edit_line(self, i=0):
        return builders.claude_tool("Edit", file_path=f"{self.outside}/src/f{i}.py", old_string="a", new_string="b")

    def capture_line(self):
        return builders.claude_tool("Write", file_path=self.root_path("storefront/capture.md"), content="notes")

    def command_line(self, command):
        return builders.claude_tool("Bash", command=command)

    def append(self, *lines):
        with open(self.transcript, "a") as handle:
            handle.write("\n".join(lines) + "\n")

    def stop(self, tool="claude", **payload):
        body = {"session_id": self.session, "transcript_path": str(self.transcript), "stop_hook_active": False}
        body.update(payload)
        return hook.handle_stop(self.cfg, tool, json.dumps(body))

    def is_block(self, answer):
        return bool(answer) and (answer.get("decision") == "block" or answer.get("continue") is False)


class EditTriggerTest(HookCase):
    def test_blocks_at_the_threshold_and_not_one_edit_before(self):
        """Regression: the checkpoint fires on substantial work only, so short exchanges are never interrupted."""
        self.append(*[self.edit_line(i) for i in range(7)])
        self.assertEqual(self.stop(), {})
        self.append(self.edit_line(7))
        answer = self.stop()
        self.assertEqual(answer["decision"], "block")

    def test_message_names_the_corpus_the_standing_file_and_the_doctor(self):
        self.append(*[self.edit_line(i) for i in range(8)])
        reason = self.stop()["reason"]
        self.assertIn(str(self.root), reason)
        self.assertIn(str(self.root / "standing-rules.md"), reason)
        self.assertIn("ctx doctor", reason)
        self.assertNotIn("{root}", reason)
        self.assertNotIn("{standing_file}", reason)

    def test_custom_message_and_threshold_come_from_config(self):
        self.load(top="nudge_min_edits = 2", tables='[hook]\nstop_message = "write it down in {root}"')
        self.append(self.edit_line(0), self.edit_line(1))
        self.assertEqual(self.stop()["reason"], f"write it down in {self.root}")

    def test_rolling_cadence_nudges_again_every_threshold_not_every_turn(self):
        """Regression: a long session is checkpointed at 8, 16, 24 edits instead of once or on every turn."""
        self.append(*[self.edit_line(i) for i in range(8)])
        self.assertTrue(self.is_block(self.stop()))
        self.assertEqual(self.stop(), {}, "same transcript, nothing new to record")
        self.append(*[self.edit_line(i) for i in range(8, 15)])
        self.assertEqual(self.stop(), {}, "15 edits is 7 beyond the last nudge")
        self.append(self.edit_line(15))
        self.assertTrue(self.is_block(self.stop()), "16 edits is 8 beyond the last nudge")

    def test_a_corpus_write_resets_the_window(self):
        """Regression: work already captured must not count toward the next nudge."""
        self.append(*[self.edit_line(i) for i in range(7)], self.capture_line(), *[self.edit_line(i) for i in range(7)])
        self.assertEqual(self.stop(), {}, "7 edits before the capture do not combine with 7 after it")
        self.append(self.edit_line(99))
        self.assertTrue(self.is_block(self.stop()))

    def test_after_a_nudge_and_a_capture_the_next_nudge_comes_after_one_threshold_not_two(self):
        """Regression: a stale marker once pushed the post-capture window from 8 edits out to 16."""
        self.append(*[self.edit_line(i) for i in range(8)])
        self.assertTrue(self.is_block(self.stop()))
        self.append(self.capture_line(), *[self.edit_line(i) for i in range(7)])
        self.assertEqual(self.stop(), {})
        self.append(self.edit_line(7))
        self.assertTrue(self.is_block(self.stop()), "8 edits since the capture must nudge")

    def test_a_large_batch_after_a_capture_still_nudges(self):
        """Regression: the window must restart even when one turn jumps past the old marker in a single step."""
        self.append(*[self.edit_line(i) for i in range(8)])
        self.assertTrue(self.is_block(self.stop()))
        self.append(self.capture_line(), *[self.edit_line(i) for i in range(12)])
        self.assertTrue(self.is_block(self.stop()))

    def test_corpus_writes_and_harness_files_are_not_edits(self):
        self.append(*[self.capture_line() for _ in range(10)],
                    *[builders.claude_tool("Write", file_path=f"{self.home}/.claude/plans/p{i}.md", content="x") for i in range(10)])
        self.assertEqual(self.stop(), {})

    def test_codex_batch_of_patched_files_counts_every_file(self):
        """Regression: one patch call touching nine files is nine edits, and the batch alone must nudge."""
        path = self.add_codex("batch", [builders.codex_patch_end(*[f"{self.outside}/f{i}.py" for i in range(9)])])
        answer = self.stop("codex", transcript_path=str(path))
        self.assertEqual(answer["continue"], False)
        self.assertIn("ctx doctor", answer["stopReason"])


class MilestoneTriggerTest(HookCase):
    def test_five_durable_actions_nudge_with_zero_file_edits(self):
        """Regression: review and merge sessions make many git actions and almost no edits, and must still be captured."""
        self.append(*[self.command_line(f"gh pr merge {n} --squash") for n in range(4)])
        self.assertEqual(self.stop(), {})
        self.append(self.command_line("git push origin main"))
        self.assertEqual(self.stop()["decision"], "block")

    def test_read_only_polling_never_counts(self):
        """Regression: a watch loop of reads once produced dozens of false milestones."""
        polls = ["gh pr view 7 --json state", "gh pr list", "gh api repos/o/r/check-runs", "git status",
                 "git log --oneline -5", "git diff", "gh api repos/o/r -X GET"]
        self.append(*[self.command_line(c) for c in polls * 4])
        self.assertEqual(self.stop(), {})

    def test_milestone_cadence_rolls_and_resets_on_capture(self):
        self.append(*[self.command_line("git commit -m x") for _ in range(5)])
        self.assertTrue(self.is_block(self.stop()))
        self.append(self.capture_line(), *[self.command_line("git commit -m y") for _ in range(4)])
        self.assertEqual(self.stop(), {})
        self.append(self.command_line("git commit -m z"))
        self.assertTrue(self.is_block(self.stop()))

    def test_milestone_pattern_table(self):
        should_match = {
            "gh pr create --fill": 1,
            "gh pr merge 5 --squash": 1,
            "gh pr edit 5 --title x": 1,
            "gh pr ready 5": 1,
            "gh pr review 5 --approve": 1,
            "gh pr close 5": 1,
            "git commit -m 'x'": 1,
            "git -C /tmp/repo commit -m x": 1,
            "git push --force-with-lease origin branch": 1,
            "git merge main": 1,
            "git rebase origin/main": 1,
            "git cherry-pick 0a1b2c3": 1,
            "git revert HEAD": 1,
            "gh api repos/o/r/pulls -X POST -f title=x": 1,
            "gh api repos/o/r/issues/1 -XPATCH": 1,
            "gh api x --method DELETE": 1,
            "git commit -m x && git push": 2,
            "gh pr create && gh pr merge 1": 2,
        }
        should_not_match = [
            "gh pr view 1", "gh pr list", "gh pr checks 1", "git status", "git log --oneline", "git diff HEAD",
            "git merge-base a b", "git commit-tree abc", "git pushd", "gh api repos/o/r/check-runs",
            "gh api x -X GET", "gh api x && curl -X POST y", "gh api x | jq . ; curl -X PUT z",
        ]
        for command, expected in should_match.items():
            with self.subTest(command=command):
                self.assertEqual(milestone_count(command), expected)
        for command in should_not_match:
            with self.subTest(command=command):
                self.assertEqual(milestone_count(command), 0)

    def test_codex_shell_commands_count_as_milestones_but_apply_patch_bodies_do_not(self):
        lines = [builders.codex_call("git commit -am wip") for _ in range(5)]
        path = self.add_codex("miles", lines)
        self.assertTrue(self.is_block(self.stop("codex", transcript_path=str(path))))
        quoting = builders.codex_call("*** Begin Patch\n+git push\n+git push\n*** End Patch\n",
                                      field="input", name="apply_patch", kind="custom_tool_call")
        other = self.add_codex("quote", [quoting] * 10)
        self.assertEqual(self.stop("codex", transcript_path=str(other), session_id="other"), {})


class DialectAndGuardTest(HookCase):
    def over_threshold(self):
        self.append(*[self.edit_line(i) for i in range(8)])

    def test_claude_and_codex_get_different_json_with_the_same_message(self):
        """Regression: answering a harness in the other dialect makes the block silently do nothing."""
        self.over_threshold()
        claude = self.stop("claude")
        codex = self.stop("codex", session_id="second-session")
        self.assertEqual(set(claude), {"decision", "reason"})
        self.assertEqual(set(codex), {"continue", "stopReason"})
        self.assertEqual(claude["reason"], codex["stopReason"])

    def test_the_tool_flag_picks_the_dialect_but_the_transcript_shape_picks_the_parser(self):
        """Regression: a Codex hook handed a Claude-shaped transcript must still count its edits."""
        self.over_threshold()
        self.assertEqual(self.stop("codex")["continue"], False)

    def test_loop_guard_never_blocks_a_stop_that_is_already_a_continuation(self):
        self.over_threshold()
        self.assertEqual(self.stop(stop_hook_active=True), {})
        self.assertTrue(self.is_block(self.stop()), "the guard must not have consumed the nudge")

    def test_kill_switch_file_silences_the_hook(self):
        self.over_threshold()
        self.state.mkdir(parents=True)
        (self.state / "disabled").write_text("")
        self.assertEqual(self.stop(), {})
        (self.state / "disabled").unlink()
        self.assertTrue(self.is_block(self.stop()))

    def test_malformed_or_incomplete_payloads_fail_open(self):
        """Regression: a hook that raises or prints garbage can wedge the harness, so every bad input answers {}."""
        for raw in ("", "not json", "[]", "null", "123", '"text"', "{}", '{"session_id": "x"}',
                    json.dumps({"transcript_path": str(self.transcript)}) + "}"):
            with self.subTest(raw=raw):
                self.assertEqual(hook.handle_stop(self.cfg, "claude", raw), {})

    def test_unknown_tool_and_missing_transcript_fail_open(self):
        self.over_threshold()
        self.assertEqual(self.stop("other-tool"), {})
        self.assertEqual(self.stop(transcript_path=str(self.tmp / "missing.jsonl")), {})

    def test_exceptions_inside_evaluation_become_an_empty_answer(self):
        from unittest import mock
        with mock.patch.object(hook, "compute_work", side_effect=RuntimeError("boom")):
            self.over_threshold()
            self.assertEqual(self.stop(), {})

    def test_session_id_may_be_absent_the_transcript_path_keys_the_marker(self):
        self.over_threshold()
        body = json.dumps({"transcript_path": str(self.transcript)})
        self.assertTrue(self.is_block(hook.handle_stop(self.cfg, "claude", body)))
        self.assertEqual(hook.handle_stop(self.cfg, "claude", body), {}, "the derived key persists the marker")

    def test_alternate_payload_key_names_are_accepted(self):
        self.over_threshold()
        body = json.dumps({"sessionId": "camel", "transcriptPath": str(self.transcript)})
        self.assertTrue(self.is_block(hook.handle_stop(self.cfg, "claude", body)))

    def test_codex_without_a_transcript_path_uses_the_newest_rollout(self):
        older = self.add_codex("old", [builders.codex_patch_end(f"{self.outside}/a.py")], age_days=3)
        newest = self.add_codex("new", [builders.codex_patch_end(*[f"{self.outside}/f{i}.py" for i in range(8)])])
        self.assertNotEqual(older, newest)
        answer = hook.handle_stop(self.cfg, "codex", json.dumps({"session_id": "cx"}))
        self.assertEqual(answer["continue"], False)

    def test_marker_stores_cumulative_totals_in_the_documented_shape(self):
        self.append(*[self.edit_line(i) for i in range(8)], *[self.command_line("git commit -m x") for _ in range(2)])
        self.stop()
        marker = json.loads((self.state / f"nudged-{self.session}").read_text())
        self.assertEqual(marker, {"e": 8, "m": 2})

    def test_a_marker_from_a_different_transcript_is_ignored(self):
        """Regression: a stale marker larger than the transcript's own totals must not silence the hook."""
        self.state.mkdir(parents=True)
        (self.state / f"nudged-{self.session}").write_text(json.dumps({"e": 500, "m": 500}))
        self.over_threshold()
        self.assertTrue(self.is_block(self.stop()))

    def test_a_hostile_session_id_cannot_write_outside_the_state_directory(self):
        self.over_threshold()
        answer = self.stop(session_id="../../escape")
        self.assertTrue(self.is_block(answer))
        self.assertFalse((self.tmp / "escape").exists())
        self.assertTrue(any(p.name.startswith("nudged-") for p in self.state.iterdir()))

    def test_only_the_shape_of_the_payload_is_remembered_not_its_content(self):
        """Regression: the diagnostic file must not keep conversation text on disk."""
        self.over_threshold()
        self.stop(last_assistant_message="a secret sentence about the quarterly plan")
        text = (self.state / "last-hook-payload-claude.json").read_text()
        self.assertIn("last_assistant_message", text)
        self.assertNotIn("quarterly", text)

    def test_unwritable_marker_stays_silent_instead_of_nagging_every_turn(self):
        self.over_threshold()
        self.state.mkdir(parents=True)
        os.mkdir(self.state / f"nudged-{self.session}")  # a directory where the marker file should go
        self.assertEqual(self.stop(), {})


if __name__ == "__main__":
    unittest.main()
