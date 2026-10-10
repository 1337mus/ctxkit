import json
import unittest

import builders
from ctxkit.transcripts import RootMatcher, classify_paths, iter_events, payload_text, scan_sessions


class TranscriptParsingBase(builders.TranscriptCase):
    def events(self, lines, tool="claude"):
        add = self.add_claude if tool == "claude" else self.add_codex
        path = add("parse", lines)
        return list(iter_events(path, RootMatcher(self.root, home=self.home)))

    def values(self, events, kind):
        return [e.value for e in events if e.kind == kind]


class ClaudeShapeTest(TranscriptParsingBase):
    def test_recorded_session_yields_reads_edits_commands_and_turns(self):
        events = self.events(self.fixture_lines("claude_session.jsonl"))
        self.assertEqual(len(self.values(events, "turn")), 12)
        self.assertEqual(self.values(events, "read"), ["README.md", "storefront/README.md"])
        self.assertEqual(self.values(events, "write"), ["storefront/cart-checkout.md"])
        self.assertEqual(self.values(events, "edit"), [f"{self.outside}/src/cart.py", f"{self.outside}/src/expiry.py"])
        self.assertEqual(len(self.values(events, "command")), 3)

    def test_edits_to_the_harness_own_config_directory_are_not_project_work(self):
        """Regression: plan and memory files the agent keeps for itself must not count toward the edit nudge."""
        events = self.events([builders.claude_tool("Write", file_path=f"{self.home}/.claude/plans/p.md", content="x")])
        self.assertEqual(self.values(events, "edit"), [])
        self.assertEqual(self.values(events, "write"), [])

    def test_notebook_edits_are_read_from_notebook_path(self):
        """Regression: NotebookEdit carries notebook_path, not file_path, and was silently never counted."""
        events = self.events([builders.claude_tool("NotebookEdit", notebook_path=f"{self.outside}/nb.ipynb")])
        self.assertEqual(self.values(events, "edit"), [f"{self.outside}/nb.ipynb"])

    def test_tool_results_and_user_lines_are_not_turns(self):
        events = self.events([builders.claude_result(), json.dumps({"type": "user", "message": {"content": "hi"}})])
        self.assertEqual(events, [])

    def test_a_user_message_that_merely_quotes_assistant_markup_is_not_a_turn(self):
        """Regression: matching the raw text '"type":"assistant"' would count quoted transcripts as turns."""
        quoted = json.dumps({"type": "user", "message": {"content": 'it printed {"type":"assistant"} somewhere'}})
        self.assertEqual(self.events([quoted]), [])

    def test_bad_lines_are_skipped_without_losing_the_good_ones(self):
        lines = ["{not json", "", "[1, 2]", builders.claude_tool("Read", file_path=self.root_path("README.md")), "null"]
        self.assertEqual(self.values(self.events(lines), "read"), ["README.md"])


class CodexShapeTest(TranscriptParsingBase):
    def test_recorded_session_yields_reads_writes_edits_commands_and_turns(self):
        events = self.events(self.fixture_lines("codex_session.jsonl"), tool="codex")
        self.assertEqual(len(self.values(events, "turn")), 2)
        self.assertEqual(sorted(set(self.values(events, "read"))),
                         ["README.md", "fulfillment/carrier-rates.md", "storefront/search-ranking.md"])
        self.assertEqual(set(self.values(events, "write")), {"fulfillment/returns-policy.md"})
        self.assertEqual(self.values(events, "edit"),
                         [f"{self.outside}/src/shipping.py", f"{self.outside}/src/shipping_test.py"])
        self.assertEqual(len(self.values(events, "command")), 5)

    def test_function_call_paths_are_read_from_the_arguments_field(self):
        events = self.events([builders.codex_call(f"cat {self.root_path('README.md')}", field="arguments")], "codex")
        self.assertEqual(self.values(events, "read"), ["README.md"])

    def test_function_call_paths_are_read_from_the_input_field_too(self):
        """Regression: Codex versions differ on arguments versus input, and reading only one counts nothing."""
        line = json.dumps({"timestamp": "t", "type": "response_item", "payload": {
            "type": "function_call", "name": "shell", "call_id": "c",
            "input": json.dumps({"command": ["bash", "-lc", f"cat {self.root_path('storefront/README.md')}"]})}})
        self.assertEqual(self.values(self.events([line], "codex"), "read"), ["storefront/README.md"])

    def test_custom_tool_call_input_string_is_scanned(self):
        events = self.events([builders.codex_call(f"rg -n cart {self.root_path('storefront/cart-checkout.md')}",
                                                  field="input", name="shell", kind="custom_tool_call")], "codex")
        self.assertEqual(self.values(events, "read"), ["storefront/cart-checkout.md"])

    def test_patch_apply_end_counts_one_edit_per_file_not_one_per_call(self):
        """Regression: Codex batches. One call that patched nine files is nine edits, not one."""
        paths = [f"{self.outside}/src/f{i}.py" for i in range(9)]
        events = self.events([builders.codex_patch_end(*paths)], "codex")
        self.assertEqual(self.values(events, "edit"), paths)

    def test_failed_patches_are_not_edits(self):
        events = self.events([builders.codex_patch_end(f"{self.outside}/a.py", success=False)], "codex")
        self.assertEqual(events, [])

    def test_patch_under_the_corpus_root_is_a_write_not_an_edit(self):
        events = self.events([builders.codex_patch_end(self.root_path("storefront/promo-codes.md"))], "codex")
        self.assertEqual(self.values(events, "write"), ["storefront/promo-codes.md"])
        self.assertEqual(self.values(events, "edit"), [])

    def test_patch_under_the_codex_config_directory_is_ignored(self):
        events = self.events([builders.codex_patch_end(f"{self.home}/.codex/memories/m.md")], "codex")
        self.assertEqual(events, [])

    def test_apply_patch_body_is_never_a_read_or_a_command(self):
        """Regression: a patch that merely quotes a corpus path or the words 'git push' is not a read or a milestone."""
        body = ("*** Begin Patch\n*** Add File: /work/shop-api/NOTES.md\n"
                f"+see {self.root_path('README.md')} and run git push\n*** End Patch\n")
        events = self.events([builders.codex_call(body, field="input", name="apply_patch", kind="custom_tool_call")], "codex")
        self.assertEqual(events, [])


class PathClassificationTest(TranscriptParsingBase):
    def classify(self, text):
        return classify_paths(text, RootMatcher(self.root, home=self.home))

    def test_redirect_target_is_a_write_and_its_source_is_a_read(self):
        reads, writes = self.classify(f"cat {self.root_path('storefront/a.md')} > {self.root_path('storefront/b.md')}")
        self.assertEqual((reads, writes), (["storefront/a.md"], ["storefront/b.md"]))

    def test_in_place_edit_and_removal_are_writes(self):
        self.assertEqual(self.classify(f"sed -i 's/a/b/' {self.root_path('x.md')}"), ([], ["x.md"]))
        self.assertEqual(self.classify(f"rm {self.root_path('x.md')}"), ([], ["x.md"]))

    def test_patch_headers_are_writes_and_quoted_paths_inside_are_ignored(self):
        text = (f"*** Begin Patch\n*** Update File: {self.root_path('a.md')}\n+see {self.root_path('b.md')}\n*** End Patch\n")
        self.assertEqual(self.classify(text), ([], ["a.md"]))

    def test_directories_and_globs_are_not_file_reads(self):
        """Regression: listing a folder or globbing it must not count as having read a file."""
        self.assertEqual(self.classify(f"ls {self.root_path('storefront')}"), ([], []))
        self.assertEqual(self.classify(f"cat {self.root}/*.md"), ([], []))

    def test_a_path_that_merely_ends_with_the_root_is_not_under_the_root(self):
        self.assertEqual(self.classify(f"cat /backup{self.root_path('README.md')}"), ([], []))

    def test_quoted_and_punctuated_paths_are_trimmed(self):
        self.assertEqual(self.classify(f'echo "{self.root_path("README.md")}".')[0], ["README.md"])
        self.assertEqual(self.classify(f"cat '{self.root_path('README.md')}'")[0], ["README.md"])

    def test_dotdot_cannot_escape_the_root(self):
        self.assertEqual(self.classify(f"cat {self.root_path('../outside.md')}"), ([], []))

    def test_home_relative_forms_resolve_when_the_root_lives_under_home(self):
        """Regression: agents write ~/... and $HOME/... as often as absolute paths."""
        inside = self.home / ".agent-context"
        inside.mkdir()
        matcher = RootMatcher(inside, home=self.home)
        for text in ("cat ~/.agent-context/README.md", "cat $HOME/.agent-context/README.md",
                     "cat ${HOME}/.agent-context/README.md", f"cat {inside}/README.md"):
            with self.subTest(text=text):
                self.assertEqual(classify_paths(text, matcher), (["README.md"], []))

    def test_payload_text_reads_both_fields_and_decodes_json(self):
        text = payload_text({"arguments": json.dumps({"cmd": "ls -la"}), "input": "echo hi"})
        self.assertIn("ls -la", text)
        self.assertIn("echo hi", text)
        self.assertEqual(payload_text({"name": "x"}), "")


class SessionScanTest(builders.TranscriptCase):
    def test_sessions_are_labelled_by_source_and_summarised(self):
        self.add_claude("a", self.fixture_lines("claude_session.jsonl"))
        self.add_codex("b", self.fixture_lines("codex_session.jsonl"))
        sessions = {s.tool: s for s in scan_sessions(self.cfg, 30)}
        self.assertEqual(sessions["claude"].turns, 12)
        self.assertEqual(sessions["claude"].reads, {"README.md", "storefront/README.md"})
        self.assertEqual(sessions["codex"].turns, 2)
        self.assertEqual(sessions["codex"].writes, {"fulfillment/returns-policy.md"})

    def test_parsing_follows_the_line_shape_not_the_source_label(self):
        """Regression: a Claude-shaped file found by the Codex glob must still be parsed, not silently ignored."""
        self.add_codex("odd", [builders.claude_tool("Read", file_path=self.root_path("README.md"))])
        (session,) = scan_sessions(self.cfg, 30)
        self.assertEqual((session.tool, session.reads), ("codex", {"README.md"}))

    def test_old_transcripts_and_disabled_sources_are_excluded(self):
        self.add_claude("fresh", [builders.claude_text()])
        self.add_claude("stale", [builders.claude_text()], age_days=40)
        self.add_codex("cx", [builders.codex_message()])
        self.assertEqual(len(scan_sessions(self.cfg, 30)), 2)
        self.load(codex_extra="enabled = false")
        self.assertEqual([s.tool for s in scan_sessions(self.cfg, 30)], ["claude"])


if __name__ == "__main__":
    unittest.main()
