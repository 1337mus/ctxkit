import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import builders
import support
from ctxkit import cli


def ctx(*argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliCase(builders.TranscriptCase):
    def setUp(self):
        super().setUp()
        self.config = str(self.write_config())


class HelpAndParsingTest(CliCase):
    def test_help_lists_every_command_and_the_lookup_order(self):
        code, out, _ = ctx("--help")
        self.assertEqual(code, 0)
        for name in ("doctor", "stats", "hook", "snapshot", "digest", "distill", "schedule", "recall", "config"):
            self.assertIn(name, out)
        self.assertIn("$CTXKIT_CONFIG", out)
        self.assertIn("~/.config/ctxkit/config.toml", out)

    def test_config_flag_works_before_or_after_the_subcommand(self):
        before = ctx("--config", self.config, "config")
        after = ctx("config", "--config", self.config)
        self.assertEqual(before[0:2], after[0:2])
        self.assertIn(f'root = "{self.root}"', before[1])

    def test_a_missing_or_invalid_config_is_a_usage_error_with_a_message(self):
        code, _, err = ctx("--config", str(self.tmp / "nope.toml"), "doctor")
        self.assertEqual(code, 2)
        self.assertIn("config error", err)
        bad = self.tmp / "bad.toml"
        bad.write_text("fat_file_bytes = 'big'")
        self.assertEqual(ctx("--config", str(bad), "doctor")[0], 2)

    def test_unknown_config_keys_warn_on_stderr_but_still_run(self):
        path = self.tmp / "typo.toml"
        path.write_text(f'root = "{self.root}"\nstate_dir = "{self.state}"\nfat_file_byts = 1\n')
        code, _, err = ctx("--config", str(path), "doctor")
        self.assertEqual(code, 0)
        self.assertIn("unknown config key: fat_file_byts", err)

    def test_non_positive_day_counts_are_rejected(self):
        self.assertEqual(ctx("--config", self.config, "stats", "--days", "0")[0], 2)
        self.assertEqual(ctx("--config", self.config, "distill", "--days", "x")[0], 2)

    def test_schedule_print_needs_exactly_one_flavour(self):
        self.assertEqual(ctx("--config", self.config, "schedule", "print")[0], 2)
        self.assertEqual(ctx("--config", self.config, "schedule", "print", "--cron", "--launchd")[0], 2)

    def test_version_flag(self):
        code, out, _ = ctx("--version")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("ctxkit "))


class CommandsThroughTheCliTest(CliCase):
    def test_doctor_exit_status_follows_the_findings(self):
        self.assertEqual(ctx("--config", self.config, "doctor")[0], 0)
        self.write("storefront/stray.md", "# Stray\n\nUpdated: 2026-03-02\n")
        code, out, _ = ctx("--config", self.config, "doctor")
        self.assertEqual(code, 1)
        self.assertIn("storefront/stray.md", out)

    def test_stats_uses_the_configured_trend_window_by_default_and_records_a_row(self):
        self.add_claude("s", [builders.claude_tool("Read", file_path=self.root_path("README.md"))] * 25)
        code, out, _ = ctx("--config", self.config, "stats")
        self.assertEqual(code, 0)
        self.assertIn("last 30 days", out)
        self.assertTrue((self.state / "compliance.tsv").exists())
        _, out, _ = ctx("--config", self.config, "stats", "--days", "7")
        self.assertIn("not recorded to the trend", out)

    def test_snapshot_digest_and_distill_run(self):
        self.init_repo()
        self.edit("storefront/promo-codes.md", "never stack", "do not stack")
        self.assertEqual(ctx("--config", self.config, "snapshot")[0], 0)
        self.assertEqual(self.head_message(), "snapshot: 1 file(s) changed")
        code, out, _ = ctx("--config", self.config, "digest")
        self.assertEqual(code, 0)
        self.assertIn("## Tripped", out)
        code, out, _ = ctx("--config", self.config, "distill", "--days", "3")
        self.assertEqual(code, 0)
        self.assertIn("ctx distill (last 3 days)", out)

    def test_schedule_print_launchd_shows_three_jobs_with_absolute_paths(self):
        code, out, _ = ctx("--config", self.config, "schedule", "print", "--launchd")
        self.assertEqual(code, 0)
        for label in ("dev.ctxkit.stats", "dev.ctxkit.snapshot", "dev.ctxkit.digest"):
            self.assertIn(label, out)
        self.assertIn(self.config, out)

    def test_recall_flow_uses_config_defaults_and_flags_override_them(self):
        command = json.dumps([sys.executable, str(support.FAKE_AGENT), "{prompt}"])
        self.config = str(self.write_config(tables=f"[recall]\nn = 4\nseed = 9\nagent_command = {command}"))
        code, out, _ = ctx("--config", self.config, "recall", "build")
        self.assertEqual(code, 0)
        self.assertIn("recall set: 4 item(s)", out)
        self.assertIn("seed 9", out)
        _, out, _ = ctx("--config", self.config, "recall", "build", "--n", "2", "--seed", "3")
        self.assertIn("recall set: 2 item(s)", out)
        self.assertIn("seed 3", out)
        code, out, _ = ctx("--config", self.config, "recall", "run")
        self.assertEqual(code, 0)
        self.assertIn("accuracy: 2/2 (100.0%)", out)
        code, out, _ = ctx("--config", self.config, "recall", "report")
        self.assertEqual(code, 0)
        self.assertIn("RECALL TREND (1 run(s))", out)


class HookThroughTheCliTest(CliCase):
    def payload(self, transcript):
        return json.dumps({"session_id": "s1", "transcript_path": str(transcript), "stop_hook_active": False})

    def over_threshold(self):
        lines = [builders.claude_tool("Edit", file_path=f"{self.outside}/f{i}.py", old_string="a", new_string="b") for i in range(8)]
        return self.add_claude("s1", lines)

    def test_blocks_in_each_dialect_and_prints_json_only(self):
        transcript = self.over_threshold()
        code, out, _ = ctx("--config", self.config, "hook", "stop", stdin=self.payload(transcript))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "block")
        code, out, _ = ctx("--config", self.config, "hook", "stop", "--tool", "codex",
                           stdin=json.dumps({"session_id": "s2", "transcript_path": str(transcript)}))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["continue"], False)

    def test_everything_that_can_go_wrong_still_exits_zero_with_an_empty_object(self):
        """Regression: a hook that fails loudly can wedge the harness that runs it."""
        transcript = self.over_threshold()
        cases = {
            "malformed stdin": (["--config", self.config, "hook", "stop"], "not json"),
            "empty stdin": (["--config", self.config, "hook", "stop"], ""),
            "unknown tool": (["--config", self.config, "hook", "stop", "--tool", "other-tool"], self.payload(transcript)),
            "bad flag": (["--config", self.config, "hook", "stop", "--bogus"], self.payload(transcript)),
            "missing config": (["--config", str(self.tmp / "nope.toml"), "hook", "stop"], self.payload(transcript)),
        }
        for name, (argv, stdin) in cases.items():
            with self.subTest(case=name):
                code, out, _ = ctx(*argv, stdin=stdin)
                self.assertEqual((code, out.strip()), (0, "{}"))

    def test_an_interactive_terminal_does_not_block_waiting_for_input(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", Tty("")):
            code = cli.main(["--config", self.config, "hook", "stop"])
        self.assertEqual((code, out.getvalue().strip()), (0, "{}"))


class RealProcessTest(CliCase):
    """One true subprocess run to prove the module entry point and exit statuses work outside the test harness."""

    def run_module(self, *args):
        env = dict(os.environ, PYTHONPATH=str(support.SRC))
        return subprocess.run([sys.executable, "-m", "ctxkit", *args], capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL)

    def test_python_dash_m_runs_and_propagates_exit_statuses(self):
        helped = self.run_module("--help")
        self.assertEqual(helped.returncode, 0)
        self.assertIn("doctor", helped.stdout)
        self.assertEqual(self.run_module("--config", self.config, "doctor").returncode, 0)
        self.write("storefront/stray.md", "# Stray\n\nUpdated: 2026-03-02\n")
        failing = self.run_module("--config", self.config, "doctor")
        self.assertEqual(failing.returncode, 1)
        self.assertIn("ORPHANS", failing.stdout)
        hook = self.run_module("--config", self.config, "hook", "stop")
        self.assertEqual((hook.returncode, hook.stdout.strip()), (0, "{}"))


if __name__ == "__main__":
    unittest.main()
