import dataclasses
import os
import tomllib
import unittest

import support  # noqa: F401  (puts ./src on sys.path)
from ctxkit.config import ConfigError, build_config, default_config, load_config


class DefaultsTest(support.WorkspaceCase):
    def test_built_in_defaults_when_no_file_exists(self):
        """Regression: a user with no config file gets the documented defaults, rooted in their home."""
        cfg = load_config()
        self.assertIsNone(cfg.source_path)
        self.assertEqual(cfg.root, self.home / ".agent-context")
        self.assertEqual(cfg.state_dir, self.home / ".local" / "state" / "ctxkit")
        self.assertEqual(cfg.digest_dir, cfg.state_dir / "digest")
        self.assertEqual(cfg.standing_files[0], "standing-rules.md")
        self.assertEqual(cfg.archive_dirname, "archive")
        self.assertEqual(cfg.excluded_dirs, ["personal"])
        self.assertEqual(
            (cfg.fat_file_bytes, cfg.regrown_dormant_days, cfg.nudge_min_edits, cfg.nudge_min_milestones,
             cfg.index_token_flag, cfg.index_entry_max_bytes, cfg.dead_weight_min_age_days,
             cfg.substantial_min_turns, cfg.trend_window_days),
            (40000, 7, 8, 5, 10000, 1500, 7, 20, 30),
        )
        self.assertTrue(cfg.claude_enabled and cfg.codex_enabled)
        self.assertTrue(cfg.snapshot_enabled)
        self.assertFalse(cfg.snapshot_sign, "unattended snapshots must not default to signing")
        self.assertEqual(cfg.notify_command, [])
        self.assertEqual(cfg.recall_agent_command, [])
        self.assertIn("{standing_file}", cfg.stop_message)

    def test_partial_file_overrides_only_the_named_keys(self):
        """Regression: setting one key must not reset the others to zero values."""
        path = self.tmp / "partial.toml"
        path.write_text('fat_file_bytes = 1234\n[snapshot]\nsign = true\n')
        cfg = load_config(str(path))
        self.assertEqual(cfg.fat_file_bytes, 1234)
        self.assertTrue(cfg.snapshot_sign)
        self.assertTrue(cfg.snapshot_enabled)
        self.assertEqual(cfg.nudge_min_edits, 8)
        self.assertEqual(cfg.source_path, path.resolve())

    def test_lookup_order_explicit_then_env_then_home_then_defaults(self):
        """Regression: precedence is --config, $CTXKIT_CONFIG, ~/.config/ctxkit/config.toml, defaults."""
        home_file = self.home / ".config" / "ctxkit" / "config.toml"
        home_file.parent.mkdir(parents=True)
        home_file.write_text("fat_file_bytes = 111\n")
        env_file = self.tmp / "env.toml"
        env_file.write_text("fat_file_bytes = 222\n")
        flag_file = self.tmp / "flag.toml"
        flag_file.write_text("fat_file_bytes = 333\n")

        self.assertEqual(load_config().fat_file_bytes, 111)
        os.environ["CTXKIT_CONFIG"] = str(env_file)
        self.assertEqual(load_config().fat_file_bytes, 222)
        self.assertEqual(load_config(str(flag_file)).fat_file_bytes, 333)
        os.environ.pop("CTXKIT_CONFIG")
        home_file.unlink()
        self.assertEqual(load_config().fat_file_bytes, 40000)

    def test_missing_explicit_or_env_file_is_an_error_not_a_silent_default(self):
        """Regression: a typo in --config must not quietly run against the wrong corpus."""
        with self.assertRaises(ConfigError):
            load_config(str(self.tmp / "nope.toml"))
        os.environ["CTXKIT_CONFIG"] = str(self.tmp / "also-nope.toml")
        with self.assertRaises(ConfigError):
            load_config()

    def test_tilde_expands_against_home(self):
        """Regression: ~ in root, state_dir and globs resolves to the current HOME."""
        path = self.tmp / "tilde.toml"
        path.write_text('root = "~/notes"\nstate_dir = "~/st"\n[sources.codex]\nglob = "~/cx/*.jsonl"\n')
        cfg = load_config(str(path))
        self.assertEqual(cfg.root, self.home / "notes")
        self.assertEqual(cfg.state_dir, self.home / "st")
        self.assertEqual(cfg.codex_glob, f"{self.home}/cx/*.jsonl")


class ValidationTest(support.WorkspaceCase):
    def _load_text(self, text: str):
        path = self.tmp / "bad.toml"
        path.write_text(text)
        return load_config(str(path))

    def test_wrong_types_name_the_offending_key(self):
        """Regression: a bad value fails loudly and says which key, rather than being coerced."""
        for text, key in [
            ('fat_file_bytes = "big"\n', "fat_file_bytes"),
            ("fat_file_bytes = 0\n", "fat_file_bytes"),
            ("excluded_dirs = 3\n", "excluded_dirs"),
            ("[snapshot]\nsign = \"yes\"\n", "snapshot.sign"),
            ('[recall]\nagent_command = "claude -p"\n', "recall.agent_command"),
        ]:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as ctx:
                    self._load_text(text)
                self.assertIn(key, str(ctx.exception))

    def test_invalid_regex_is_rejected_at_load_time(self):
        """Regression: a broken pattern must fail at startup, not midway through a doctor run."""
        with self.assertRaises(ConfigError):
            self._load_text('identifier_patterns = ["(unclosed"]\n')
        with self.assertRaises(ConfigError):
            self._load_text('rule_lead_pattern = "["\n')

    def test_malformed_toml_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            self._load_text("this is = = not toml")

    def test_unknown_keys_are_reported_as_warnings_not_errors(self):
        """Regression: a misspelled key is surfaced, but does not stop the tool."""
        cfg = self._load_text('fat_file_byts = 5\n[snapshot]\nsigning = true\n[mystery]\nx = 1\n')
        self.assertEqual(cfg.fat_file_bytes, 40000)
        joined = " ".join(cfg.warnings)
        self.assertIn("fat_file_byts", joined)
        self.assertIn("snapshot.signing", joined)
        self.assertIn("mystery", joined)

    def test_seed_may_be_zero_or_negative(self):
        cfg = self._load_text("[recall]\nseed = 0\n")
        self.assertEqual(cfg.recall_seed, 0)


class PatternsTest(support.WorkspaceCase):
    def test_default_pattern_finds_ticket_keys_and_skips_known_false_positives(self):
        """Regression: encoding names and hash names must not be tracked as tickets."""
        cfg = default_config()
        text = "Fixed SHOP-123 and ABC1-9 using UTF-8, SHA-256 and ISO-8601 on 2026-03-02."
        self.assertEqual(cfg.identifiers(text), ["SHOP-123", "ABC1-9"])

    def test_custom_patterns_and_ignore_list_are_honoured(self):
        """Regression: extra patterns (for example hash-style ids) apply alongside the default."""
        path = self.tmp / "ids.toml"
        path.write_text(
            'identifier_patterns = ["\\\\b[A-Z]{3}-\\\\d+\\\\b", "build-[0-9a-f]{7}"]\n'
            'identifier_ignore = ["ZZZ-1"]\n'
        )
        cfg = load_config(str(path))
        self.assertEqual(cfg.identifiers("ZZZ-1 AAA-2 build-0a1b2c3 SHOP-9"), ["AAA-2", "build-0a1b2c3"])

    def test_rule_lead_matches_bullets_and_numbers_but_not_short_leads(self):
        cfg = default_config()
        self.assertEqual(cfg.rule_lead("- **Check the index before searching** because it is cheap."),
                         "Check the index before searching")
        self.assertEqual(cfg.rule_lead("  3a. **Keep the ticket key on every line you rewrite** now"),
                         "Keep the ticket key on every line you rewrite")
        self.assertIsNone(cfg.rule_lead("- **Short** lead"))
        self.assertIsNone(cfg.rule_lead("Plain paragraph with **bold words inside it** here."))

    def test_rule_lead_pattern_without_a_group_uses_the_whole_match(self):
        path = self.tmp / "lead.toml"
        path.write_text('rule_lead_pattern = "^RULE: .+$"\n')
        cfg = load_config(str(path))
        self.assertEqual(cfg.rule_lead("RULE: always log the cause"), "RULE: always log the cause")


class DescribeTest(support.WorkspaceCase):
    def test_effective_config_rendering_round_trips_through_the_loader(self):
        """Regression: `ctx config` output must itself be a valid config that reproduces the same settings."""
        original = self.load(top='fat_file_bytes = 999\nexcluded_dirs = ["personal", "scratch"]',
                             tables='[recall]\nagent_command = ["agent", "{prompt}"]\nseed = 7')
        reparsed = build_config(tomllib.loads(original.to_toml()), None, self.home)
        for field in dataclasses.fields(original):
            if field.name in ("source_path", "warnings"):
                continue
            with self.subTest(field=field.name):
                self.assertEqual(getattr(reparsed, field.name), getattr(original, field.name))


if __name__ == "__main__":
    unittest.main()
