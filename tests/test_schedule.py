import contextlib
import io
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import support
from ctxkit import schedule
from ctxkit.schedule import Context, RENDERERS, default_path_env, resolve_command, systemd_quote

TEMPLATES = Path(__file__).resolve().parents[1] / "scheduling"
SAMPLE = Context(
    command=("/usr/local/bin/ctx",),
    config_path="/home/you/.config/ctxkit/config.toml",
    home="/home/you",
    state_dir="/home/you/.local/state/ctxkit",
    path_env="/usr/local/bin:/usr/bin:/bin",
)


def plist_dict(text):
    root = ET.fromstring(text)
    node = root.find("dict")
    items = list(node)
    out = {}
    for key, value in zip(items[::2], items[1::2]):
        if value.tag == "array":
            out[key.text] = [e.text for e in value]
        elif value.tag == "dict":
            inner = list(value)
            out[key.text] = {k.text: (int(v.text) if v.tag == "integer" else v.text) for k, v in zip(inner[::2], inner[1::2])}
        else:
            out[key.text] = value.text
    return out


class TemplatesStayInSyncTest(unittest.TestCase):
    def test_committed_example_files_are_exactly_what_the_renderer_produces(self):
        """Regression: the example templates in the repo must not drift from what `ctx schedule print` emits."""
        expected = {}
        for sub, kind in (("launchd", "launchd"), ("systemd", "systemd"), ("cron", "cron")):
            for rendered in RENDERERS[kind](SAMPLE):
                expected[(sub, rendered.filename)] = rendered.content
        actual = {(d.name, f.name): f.read_text() for d in TEMPLATES.iterdir() if d.is_dir() for f in d.iterdir()}
        self.assertEqual(set(actual), set(expected))
        for key, content in expected.items():
            with self.subTest(file=key):
                self.assertEqual(actual[key], content)


class LaunchdTest(unittest.TestCase):
    def files(self, ctx=SAMPLE):
        return {r.filename: plist_dict(r.content) for r in RENDERERS["launchd"](ctx)}

    def test_one_well_formed_plist_per_job_with_labels_and_absolute_arguments(self):
        files = self.files()
        self.assertEqual(sorted(files), ["dev.ctxkit.digest.plist", "dev.ctxkit.snapshot.plist", "dev.ctxkit.stats.plist"])
        for name, data in files.items():
            with self.subTest(job=name):
                self.assertEqual(data["Label"] + ".plist", name)
                self.assertTrue(os.path.isabs(data["ProgramArguments"][0]))
                self.assertEqual(data["ProgramArguments"][1:3], ["--config", "/home/you/.config/ctxkit/config.toml"])
                self.assertEqual(data["EnvironmentVariables"]["HOME"], "/home/you")
                self.assertIn("/usr/bin", data["EnvironmentVariables"]["PATH"])
                self.assertTrue(os.path.isabs(data["StandardOutPath"]))

    def test_schedules_are_daily_hourly_and_weekly(self):
        files = self.files()
        self.assertEqual(files["dev.ctxkit.stats.plist"]["StartCalendarInterval"], {"Hour": 9, "Minute": 5})
        self.assertEqual(files["dev.ctxkit.snapshot.plist"]["StartCalendarInterval"], {"Minute": 17})
        self.assertEqual(files["dev.ctxkit.digest.plist"]["StartCalendarInterval"], {"Weekday": 1, "Hour": 9, "Minute": 15})

    def test_the_subcommand_comes_after_the_global_config_flag(self):
        args = self.files()["dev.ctxkit.snapshot.plist"]["ProgramArguments"]
        self.assertEqual(args[-1], "snapshot")
        self.assertLess(args.index("--config"), args.index("snapshot"))

    def test_xml_special_characters_in_paths_are_escaped(self):
        ctx = Context(command=("/opt/a&b/ctx",), config_path=None, home="/home/you", state_dir="/s", path_env="/bin")
        data = self.files(ctx)["dev.ctxkit.stats.plist"]
        self.assertEqual(data["ProgramArguments"], ["/opt/a&b/ctx", "stats"])


class JobSelectionTest(unittest.TestCase):
    def test_digest_notifies_only_when_a_notify_command_is_configured(self):
        quiet = {r.filename: r.content for r in RENDERERS["systemd"](SAMPLE)}
        self.assertNotIn("--notify", quiet["ctxkit-digest.service"])
        loud = Context(**{**SAMPLE.__dict__, "notify": True})
        noisy = {r.filename: r.content for r in RENDERERS["systemd"](loud)}
        self.assertIn("digest --notify", noisy["ctxkit-digest.service"])
        self.assertNotIn("--notify", noisy["ctxkit-stats.service"])

    def test_snapshot_job_is_omitted_when_snapshots_are_disabled(self):
        ctx = Context(**{**SAMPLE.__dict__, "snapshot_enabled": False})
        for kind, renderer in RENDERERS.items():
            with self.subTest(kind=kind):
                text = "".join(r.content + r.filename for r in renderer(ctx))
                self.assertNotIn("snapshot", text)
                self.assertIn("stats", text)

    def test_no_config_flag_when_running_on_built_in_defaults(self):
        ctx = Context(**{**SAMPLE.__dict__, "config_path": None})
        for rendered in RENDERERS["cron"](ctx):
            self.assertNotIn("--config", rendered.content)


class SystemdTest(unittest.TestCase):
    def test_each_job_gets_a_service_and_a_persistent_timer(self):
        files = {r.filename: r.content for r in RENDERERS["systemd"](SAMPLE)}
        self.assertEqual(len(files), 6)
        self.assertIn("OnCalendar=*-*-* 09:05:00", files["ctxkit-stats.timer"])
        self.assertIn("OnCalendar=*-*-* *:17:00", files["ctxkit-snapshot.timer"])
        self.assertIn("OnCalendar=Mon *-*-* 09:15:00", files["ctxkit-digest.timer"])
        for name, content in files.items():
            if name.endswith(".timer"):
                self.assertIn("Persistent=true", content)
                self.assertIn("WantedBy=timers.target", content)
            else:
                self.assertIn("Type=oneshot", content)
                self.assertIn("ExecStart=/usr/local/bin/ctx ", content)

    def test_quoting_protects_spaces_percent_and_dollar(self):
        """Regression: an unquoted path with a space or a stray % silently changes what systemd runs."""
        self.assertEqual(systemd_quote("/usr/bin/ctx"), "/usr/bin/ctx")
        self.assertEqual(systemd_quote("/my files/ctx"), '"/my files/ctx"')
        self.assertEqual(systemd_quote("100%"), '"100%%"')
        self.assertEqual(systemd_quote("$HOME/x"), '"$$HOME/x"')
        self.assertEqual(systemd_quote('say "hi"'), '"say \\"hi\\""')


class CronTest(unittest.TestCase):
    def lines(self, ctx=SAMPLE):
        (rendered,) = RENDERERS["cron"](ctx)
        return [l for l in rendered.content.splitlines() if l and not l.startswith("#")]

    def test_environment_and_three_entries_with_absolute_commands(self):
        lines = self.lines()
        self.assertEqual(lines[0], "PATH=/usr/local/bin:/usr/bin:/bin")
        entries = [l for l in lines if l[0].isdigit()]
        self.assertEqual([" ".join(e.split()[:5]) for e in entries], ["5 9 * * *", "17 * * * *", "15 9 * * 1"])
        for entry in entries:
            self.assertEqual(entry.split()[5], "/usr/local/bin/ctx")
            self.assertTrue(entry.endswith("2>&1"))

    def test_paths_with_spaces_or_percent_are_quoted_and_escaped(self):
        ctx = Context(**{**SAMPLE.__dict__, "command": ("/my tools/ctx",), "state_dir": "/data/100%/state"})
        entry = [l for l in self.lines(ctx) if l[0].isdigit()][0]
        self.assertIn("'/my tools/ctx'", entry)
        self.assertIn("100\\%", entry)
        self.assertNotIn("100%/", entry)


class ResolutionTest(unittest.TestCase):
    def test_a_script_named_ctx_is_resolved_to_an_absolute_path(self):
        self.assertEqual(resolve_command("/tmp/bin/ctx"), (str(Path("/tmp/bin/ctx").resolve()),))

    def test_running_as_a_module_names_the_interpreter_and_never_searches_the_path(self):
        """Regression: an unrelated program called ctx on PATH was being scheduled in place of this tool."""
        with mock.patch("shutil.which", return_value="/opt/other-tool/bin/ctx") as which:
            self.assertEqual(resolve_command("/x/ctxkit/__main__.py"), (sys.executable, "-m", "ctxkit"))
            which.assert_not_called()

    def test_path_includes_git_and_the_tool_without_duplicates(self):
        value = default_path_env(("/opt/tools/ctx",), which=lambda name: "/opt/git/bin/git")
        parts = value.split(":")
        self.assertIn("/opt/git/bin", parts)
        self.assertIn("/opt/tools", parts)
        self.assertEqual(len(parts), len(set(parts)))
        self.assertTrue({"/usr/bin", "/bin"} <= set(parts))


class PrintOnlyTest(support.WorkspaceCase):
    def test_printing_never_installs_or_creates_anything(self):
        """Regression: `schedule print` is a preview. It must not touch scheduler folders or the state directory."""
        for kind in RENDERERS:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(schedule.run(self.cfg, kind, command=("/usr/local/bin/ctx",), path_env="/usr/bin:/bin"), 0)
            self.assertTrue(out.getvalue().strip(), kind)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse(self.state.exists())

    def test_output_uses_the_loaded_config_and_state_directory(self):
        path = self.write_config()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            schedule.run(self.cfg, "launchd", command=("/usr/local/bin/ctx",), path_env="/usr/bin")
        text = out.getvalue()
        self.assertIn(str(path.resolve()), text)
        self.assertIn(str(self.state / "logs" / "stats.log"), text)
        self.assertIn(f"{self.home}/Library/LaunchAgents", text)

    def test_every_line_that_is_not_file_content_is_a_comment(self):
        ctx = SAMPLE
        text = schedule.format_output("launchd", ctx)
        self.assertTrue(text.startswith("# Save each file in /home/you/Library/LaunchAgents"))
        self.assertIn("# ---- file: dev.ctxkit.stats.plist ----", text)
        self.assertIn("launchctl bootstrap gui/$(id -u)", text)
        cron = schedule.format_output("cron", ctx)
        self.assertNotIn("---- file", cron)


if __name__ == "__main__":
    unittest.main()
