"""Configuration: TOML file lookup, validation and built-in defaults.

Lookup order, first hit wins:

1. ``--config PATH`` (must exist)
2. ``$CTXKIT_CONFIG`` (must exist when set)
3. ``~/.config/ctxkit/config.toml`` (used only if present)
4. built-in defaults

An explicit path that does not exist is an error rather than a silent fallback,
because running against the wrong corpus root is worse than refusing to run.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Mapping

DEFAULT_ROOT = "~/.agent-context"
DEFAULT_STATE_DIR = "~/.local/state/ctxkit"
DEFAULT_CLAUDE_GLOB = "~/.claude/projects/*/*.jsonl"
DEFAULT_CODEX_GLOB = "~/.codex/sessions/*/*/*/rollout-*.jsonl"

# One generic ticket-key shape: two to ten capital letters or digits starting
# with a letter, a hyphen, then digits. Matches SHOP-123 and ABC1-42.
DEFAULT_IDENTIFIER_PATTERN = r"\b[A-Z][A-Z0-9]{1,9}-\d+\b"

# Strings that match the generic pattern but are not tickets.
DEFAULT_IDENTIFIER_IGNORE = [
    "UTF-8", "UTF-16", "UTF-32",
    "SHA-1", "SHA-224", "SHA-256", "SHA-384", "SHA-512",
    "ISO-8601", "ISO-8859",
    "AES-128", "AES-256", "RSA-2048", "RSA-4096",
    "HTTP-2", "HTTP-3", "COVID-19",
]

# A rule is a bullet or numbered item that opens with a bolded lead of at least
# twelve characters. Group 1 is the lead text.
DEFAULT_RULE_LEAD_PATTERN = r"^(?:[-*]|\d+[a-z]?\.)\s+\*\*(.{12,}?)\*\*"

DEFAULT_STOP_MESSAGE = (
    "This session changed code or repository state but did not update the context corpus at {root}. "
    "Before stopping, record durable context there: decisions, outcomes, gotchas and follow-up risks. "
    "Prefer updating an existing file over creating a new one. "
    "Then ask whether anything here is a lesson that would apply on a different project. "
    "If so, promote it to {standing_file} as one short imperative rule, and strengthen an existing rule "
    "instead of appending a near duplicate. "
    "Finally run `ctx doctor` and fix anything it reports. "
    "If there is genuinely nothing durable to record, say so in one line and stop."
)


class ConfigError(Exception):
    """Raised for an unreadable, malformed or invalid configuration."""


@dataclass
class Config:
    """Fully resolved configuration. Paths are absolute and ``~`` is expanded."""

    root: Path
    state_dir: Path
    standing_files: list[str]
    archive_dirname: str
    excluded_dirs: list[str]
    identifier_patterns: list[str]
    identifier_ignore: list[str]
    rule_lead_pattern: str
    fat_file_bytes: int
    regrown_dormant_days: int
    nudge_min_edits: int
    nudge_min_milestones: int
    index_token_flag: int
    index_entry_max_bytes: int
    dead_weight_min_age_days: int
    substantial_min_turns: int
    trend_window_days: int
    claude_enabled: bool
    claude_glob: str
    codex_enabled: bool
    codex_glob: str
    snapshot_enabled: bool
    snapshot_sign: bool
    digest_dir: Path
    notify_command: list[str]
    stop_message: str
    recall_agent_command: list[str]
    recall_n: int
    recall_seed: int
    recall_timeout_seconds: int
    source_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    # ---- derived helpers -------------------------------------------------

    @cached_property
    def _identifier_regexes(self) -> list[re.Pattern[str]]:
        return [re.compile(p) for p in self.identifier_patterns]

    @cached_property
    def _rule_lead_regex(self) -> re.Pattern[str]:
        return re.compile(self.rule_lead_pattern)

    @cached_property
    def _ignore_set(self) -> frozenset[str]:
        return frozenset(self.identifier_ignore)

    @property
    def standing_file(self) -> str:
        """The promotion target: the first entry of ``standing_files``."""
        return self.standing_files[0] if self.standing_files else ""

    def identifiers(self, text: str) -> list[str]:
        """Return identifiers found in ``text`` in order of appearance, minus the ignore list."""
        hits: list[tuple[int, str]] = []
        for regex in self._identifier_regexes:
            for match in regex.finditer(text):
                value = match.group(0)
                if value and value not in self._ignore_set:
                    hits.append((match.start(), value))
        hits.sort(key=lambda item: item[0])
        return [value for _, value in hits]

    def rule_lead(self, line: str) -> str | None:
        """Return the bolded lead of a rule line, or None when the line is not a rule."""
        match = self._rule_lead_regex.match(line.strip())
        if not match:
            return None
        lead = match.group(1) if self._rule_lead_regex.groups else match.group(0)
        return lead.strip()

    def to_toml(self) -> str:
        """Render the effective configuration as TOML, for ``ctx config``."""
        j = json.dumps
        lines = [
            f"# loaded from: {self.source_path if self.source_path else 'built-in defaults'}",
            f"root = {j(str(self.root))}",
            f"state_dir = {j(str(self.state_dir))}",
            f"standing_files = {j(self.standing_files)}",
            f"archive_dirname = {j(self.archive_dirname)}",
            f"excluded_dirs = {j(self.excluded_dirs)}",
            f"identifier_patterns = {j(self.identifier_patterns)}",
            f"identifier_ignore = {j(self.identifier_ignore)}",
            f"rule_lead_pattern = {j(self.rule_lead_pattern)}",
            f"fat_file_bytes = {self.fat_file_bytes}",
            f"regrown_dormant_days = {self.regrown_dormant_days}",
            f"nudge_min_edits = {self.nudge_min_edits}",
            f"nudge_min_milestones = {self.nudge_min_milestones}",
            f"index_token_flag = {self.index_token_flag}",
            f"index_entry_max_bytes = {self.index_entry_max_bytes}",
            f"dead_weight_min_age_days = {self.dead_weight_min_age_days}",
            f"substantial_min_turns = {self.substantial_min_turns}",
            f"trend_window_days = {self.trend_window_days}",
            "",
            "[sources.claude]",
            f"enabled = {j(self.claude_enabled)}",
            f"glob = {j(self.claude_glob)}",
            "",
            "[sources.codex]",
            f"enabled = {j(self.codex_enabled)}",
            f"glob = {j(self.codex_glob)}",
            "",
            "[snapshot]",
            f"enabled = {j(self.snapshot_enabled)}",
            f"sign = {j(self.snapshot_sign)}",
            "",
            "[digest]",
            f"dir = {j(str(self.digest_dir))}",
            f"notify_command = {j(self.notify_command)}",
            "",
            "[hook]",
            f"stop_message = {j(self.stop_message)}",
            "",
            "[recall]",
            f"agent_command = {j(self.recall_agent_command)}",
            f"n = {self.recall_n}",
            f"seed = {self.recall_seed}",
            f"timeout_seconds = {self.recall_timeout_seconds}",
        ]
        return "\n".join(lines) + "\n"


# ---- loading ---------------------------------------------------------------


def _home(environ: Mapping[str, str] | None) -> Path:
    if environ is not None and environ.get("HOME"):
        return Path(environ["HOME"])
    return Path.home()


def locate_config(explicit: str | os.PathLike[str] | None = None,
                  environ: Mapping[str, str] | None = None) -> Path | None:
    """Return the config file to load, or None when built-in defaults apply."""
    env = os.environ if environ is None else environ
    if explicit:
        path = Path(os.path.expanduser(str(explicit)))
        if not path.is_file():
            raise ConfigError(f"--config file not found: {path}")
        return path.resolve()
    from_env = env.get("CTXKIT_CONFIG", "").strip()
    if from_env:
        path = Path(os.path.expanduser(from_env))
        if not path.is_file():
            raise ConfigError(f"$CTXKIT_CONFIG points at a missing file: {path}")
        return path.resolve()
    default = _home(env) / ".config" / "ctxkit" / "config.toml"
    if default.is_file():
        return default.resolve()
    return None


class _Reader:
    """Typed access to a parsed TOML document that also tracks which keys were read."""

    def __init__(self, data: dict[str, Any], home: Path) -> None:
        self.data = data
        self.home = home
        self.requested: set[tuple[str, str]] = set()

    def _table(self, section: str) -> dict[str, Any]:
        table: Any = self.data
        if section:
            for part in section.split("."):
                table = table.get(part, {}) if isinstance(table, dict) else {}
                if not isinstance(table, dict):
                    raise ConfigError(f"[{section}] must be a table")
        return table

    def _raw(self, section: str, key: str) -> tuple[bool, Any, str]:
        self.requested.add((section, key))
        label = f"{section}.{key}" if section else key
        table = self._table(section)
        return key in table, table.get(key), label

    def _expand(self, value: str) -> str:
        if value == "~" or value.startswith("~/"):
            return str(self.home) + value[1:]
        return os.path.expanduser(value)

    def path(self, section: str, key: str, default: str) -> Path:
        present, raw, label = self._raw(section, key)
        value = raw if present else default
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{label} must be a non-empty string path")
        return Path(os.path.abspath(self._expand(value.strip())))

    def glob(self, section: str, key: str, default: str) -> str:
        present, raw, label = self._raw(section, key)
        value = raw if present else default
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{label} must be a non-empty string glob")
        return self._expand(value.strip())

    def string(self, section: str, key: str, default: str) -> str:
        present, raw, label = self._raw(section, key)
        if not present:
            return default
        if not isinstance(raw, str):
            raise ConfigError(f"{label} must be a string")
        return raw

    def integer(self, section: str, key: str, default: int, minimum: int | None = 1) -> int:
        present, raw, label = self._raw(section, key)
        if not present:
            return default
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ConfigError(f"{label} must be an integer")
        if minimum is not None and raw < minimum:
            raise ConfigError(f"{label} must be at least {minimum}")
        return raw

    def boolean(self, section: str, key: str, default: bool) -> bool:
        present, raw, label = self._raw(section, key)
        if not present:
            return default
        if not isinstance(raw, bool):
            raise ConfigError(f"{label} must be true or false")
        return raw

    def strings(self, section: str, key: str, default: list[str], non_empty: bool = False) -> list[str]:
        present, raw, label = self._raw(section, key)
        if not present:
            return list(default)
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ConfigError(f"{label} must be a list of strings")
        if non_empty and not raw:
            raise ConfigError(f"{label} must not be empty")
        return list(raw)

    def regex(self, section: str, key: str, default: str) -> str:
        value = self.string(section, key, default)
        label = f"{section}.{key}" if section else key
        try:
            re.compile(value)
        except re.error as exc:
            raise ConfigError(f"{label} is not a valid regular expression: {exc}") from exc
        return value

    def regexes(self, section: str, key: str, default: list[str]) -> list[str]:
        values = self.strings(section, key, default)
        label = f"{section}.{key}" if section else key
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise ConfigError(f"{label} has an invalid regular expression {value!r}: {exc}") from exc
        return values

    def unknown_keys(self) -> list[str]:
        """Dotted names of keys present in the document that no reader asked for."""
        known_sections = {s for s, _ in self.requested if s}
        known_prefixes = {".".join(s.split(".")[: i + 1]) for s in known_sections for i in range(len(s.split(".")))}
        found: list[str] = []

        def walk(table: dict[str, Any], prefix: str) -> None:
            for key, value in table.items():
                dotted = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    if dotted in known_prefixes:
                        walk(value, dotted)
                    else:
                        found.append(dotted)
                elif (prefix, key) not in self.requested:
                    found.append(dotted)

        walk(self.data, "")
        return sorted(found)


def build_config(data: dict[str, Any], source: Path | None, home: Path) -> Config:
    """Validate a parsed TOML document and fill defaults."""
    r = _Reader(data, home)
    state_dir = r.path("", "state_dir", DEFAULT_STATE_DIR)
    digest_dir = r.path("digest", "dir", str(state_dir / "digest"))
    cfg = Config(
        root=r.path("", "root", DEFAULT_ROOT),
        state_dir=state_dir,
        standing_files=r.strings("", "standing_files", ["standing-rules.md"]),
        archive_dirname=r.string("", "archive_dirname", "archive"),
        excluded_dirs=r.strings("", "excluded_dirs", ["personal"]),
        identifier_patterns=r.regexes("", "identifier_patterns", [DEFAULT_IDENTIFIER_PATTERN]),
        identifier_ignore=r.strings("", "identifier_ignore", DEFAULT_IDENTIFIER_IGNORE),
        rule_lead_pattern=r.regex("", "rule_lead_pattern", DEFAULT_RULE_LEAD_PATTERN),
        fat_file_bytes=r.integer("", "fat_file_bytes", 40000),
        regrown_dormant_days=r.integer("", "regrown_dormant_days", 7),
        nudge_min_edits=r.integer("", "nudge_min_edits", 8),
        nudge_min_milestones=r.integer("", "nudge_min_milestones", 5),
        index_token_flag=r.integer("", "index_token_flag", 10000),
        index_entry_max_bytes=r.integer("", "index_entry_max_bytes", 1500),
        dead_weight_min_age_days=r.integer("", "dead_weight_min_age_days", 7),
        substantial_min_turns=r.integer("", "substantial_min_turns", 20),
        trend_window_days=r.integer("", "trend_window_days", 30),
        claude_enabled=r.boolean("sources.claude", "enabled", True),
        claude_glob=r.glob("sources.claude", "glob", DEFAULT_CLAUDE_GLOB),
        codex_enabled=r.boolean("sources.codex", "enabled", True),
        codex_glob=r.glob("sources.codex", "glob", DEFAULT_CODEX_GLOB),
        snapshot_enabled=r.boolean("snapshot", "enabled", True),
        snapshot_sign=r.boolean("snapshot", "sign", False),
        digest_dir=digest_dir,
        notify_command=r.strings("digest", "notify_command", []),
        stop_message=r.string("hook", "stop_message", DEFAULT_STOP_MESSAGE),
        recall_agent_command=r.strings("recall", "agent_command", []),
        recall_n=r.integer("recall", "n", 20),
        recall_seed=r.integer("recall", "seed", 1, minimum=None),
        recall_timeout_seconds=r.integer("recall", "timeout_seconds", 300),
        source_path=source,
    )
    if not cfg.archive_dirname.strip() or "/" in cfg.archive_dirname:
        raise ConfigError("archive_dirname must be a single folder name")
    cfg.warnings = [f"unknown config key: {k}" for k in r.unknown_keys()]
    return cfg


def load_config(explicit: str | os.PathLike[str] | None = None,
                environ: Mapping[str, str] | None = None) -> Config:
    """Locate, parse and validate the configuration. Raises ConfigError on any problem."""
    path = locate_config(explicit, environ)
    data: dict[str, Any] = {}
    if path is not None:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
    return build_config(data, path, _home(environ))


def default_config(environ: Mapping[str, str] | None = None) -> Config:
    """Built-in defaults, ignoring any config file."""
    return build_config({}, None, _home(environ))


def ensure_state_dir(cfg: Config) -> Path:
    """Create the state directory (mode 0700 when newly created) and return it."""
    if not cfg.state_dir.exists():
        cfg.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return cfg.state_dir
