import dataclasses
import re
import tomllib
import unittest
from pathlib import Path

import support
from ctxkit.config import build_config, default_config

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "config.toml"

DOCUMENTED_KEYS = {
    "root", "state_dir", "standing_files", "archive_dirname", "excluded_dirs", "identifier_patterns",
    "identifier_ignore", "rule_lead_pattern", "fat_file_bytes", "regrown_dormant_days", "nudge_min_edits",
    "nudge_min_milestones", "index_token_flag", "index_entry_max_bytes", "dead_weight_min_age_days",
    "substantial_min_turns", "trend_window_days",
    "sources.claude.enabled", "sources.claude.glob", "sources.codex.enabled", "sources.codex.glob",
    "snapshot.enabled", "snapshot.sign", "digest.dir", "digest.notify_command", "hook.stop_message",
    "recall.agent_command", "recall.n", "recall.seed", "recall.timeout_seconds",
}


def flatten(table, prefix=""):
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from flatten(value, dotted + ".")
        else:
            yield dotted


class ExampleConfigTest(support.WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.data = tomllib.loads(EXAMPLE.read_text())
        self.example = build_config(self.data, EXAMPLE, self.home)
        self.defaults = default_config()

    def test_example_documents_every_key_and_nothing_unknown(self):
        """Regression: a new config key must be added to the example, and a renamed one must not linger."""
        self.assertEqual(set(flatten(self.data)), DOCUMENTED_KEYS)
        self.assertEqual(self.example.warnings, [])

    def test_example_values_equal_the_built_in_defaults_except_the_agent_command(self):
        """Regression: the documented default must be the real default."""
        for field in dataclasses.fields(self.defaults):
            if field.name in ("source_path", "warnings", "recall_agent_command"):
                continue
            with self.subTest(key=field.name):
                self.assertEqual(getattr(self.example, field.name), getattr(self.defaults, field.name))

    def test_every_key_has_a_comment_directly_above_it(self):
        lines = EXAMPLE.read_text().splitlines()
        for number, line in enumerate(lines):
            if re.match(r"^[a-z_]+ = ", line):
                with self.subTest(line=line[:40]):
                    self.assertTrue(lines[number - 1].startswith("#"), f"no comment above: {line[:50]}")

    def test_example_shows_a_read_only_command_for_claude_and_for_codex(self):
        command = self.example.recall_agent_command
        # Regression: with {prompt} last, claude's variadic --allowedTools swallowed it
        # and every recall run failed in about a second with "Input must be provided".
        self.assertEqual(command[:3], ["claude", "-p", "{prompt}"])
        self.assertIn("json", command)
        tools_at = command.index("--allowedTools")
        self.assertGreater(tools_at, command.index("{prompt}"))
        self.assertEqual(command[tools_at + 1], "Read,Grep,Glob")
        text = EXAMPLE.read_text()
        self.assertIn('agent_command = ["codex", "exec", "--sandbox", "read-only", "{prompt}"]', text)

    def test_every_default_has_its_value_stated_in_a_comment(self):
        text = EXAMPLE.read_text()
        for needle in ("Default 40000", "Default 7", "Default 8", "Default 5", "Default 10000", "Default 1500",
                       "Default 20", "Default 30", 'Default "archive"', 'Default ["personal"]',
                       "Default ~/.agent-context", "Default ~/.local/state/ctxkit"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
