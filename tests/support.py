"""Shared helpers for the test suite.

Importing this module puts ``./src`` on ``sys.path`` so the suite runs with a plain
``python -m unittest discover -s tests`` and no prior install.

Every test gets an isolated workspace: its own HOME, its own empty global git
config, a copy of the fixture corpus, and a git repository built in ``setUp``.
Nothing reads or writes the real home directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_AGENT = FIXTURES / "fake_agent.py"

from ctxkit.config import Config, load_config  # noqa: E402
from ctxkit.corpus import Corpus  # noqa: E402


def git(root: Path, *args: str) -> str:
    """Run git in ``root`` and return stdout. Raises on failure so a broken fixture is loud."""
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return proc.stdout


class WorkspaceCase(unittest.TestCase):
    """Base class: a temp workspace with a corpus copy, state dir and transcript dirs."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="ctxkit-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        empty_git_config = self.tmp / "gitconfig"
        empty_git_config.write_text("")
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": str(empty_git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CTXKIT_CONFIG", None)
        os.environ.pop("XDG_CONFIG_HOME", None)

        self.root = self.tmp / "corpus"
        shutil.copytree(FIXTURES / "corpus", self.root)
        self.state = self.tmp / "state"
        self.claude_dir = self.tmp / "claude-projects"
        self.codex_dir = self.tmp / "codex-sessions"
        self.cfg: Config = self.load()
        self.corpus = Corpus(self.cfg)

    # ---- configuration -----------------------------------------------------

    def write_config(self, top: str = "", tables: str = "", claude_extra: str = "", codex_extra: str = "") -> Path:
        """Write a config file pointing at this workspace.

        ``top`` goes before any table header, ``tables`` after the source tables, and the
        ``*_extra`` strings extend the two ``[sources.*]`` tables.
        """
        text = (
            f'root = "{self.root}"\n'
            f'state_dir = "{self.state}"\n'
            f"{top}\n"
            "[sources.claude]\n"
            f'glob = "{self.claude_dir}/*/*.jsonl"\n'
            f"{claude_extra}\n"
            "[sources.codex]\n"
            f'glob = "{self.codex_dir}/*/*/*/rollout-*.jsonl"\n'
            f"{codex_extra}\n"
            f"{tables}\n"
        )
        path = self.tmp / "config.toml"
        path.write_text(text)
        return path

    def load(self, top: str = "", tables: str = "", claude_extra: str = "", codex_extra: str = "") -> Config:
        """Load a workspace config and refresh ``self.cfg`` and ``self.corpus``."""
        cfg = load_config(str(self.write_config(top, tables, claude_extra, codex_extra)))
        self.cfg = cfg
        self.corpus = Corpus(cfg)
        return cfg

    # ---- corpus manipulation ----------------------------------------------

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.corpus = Corpus(self.cfg)
        return path

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text()

    def edit(self, rel: str, old: str, new: str) -> None:
        """Replace ``old`` with ``new`` in a corpus file, asserting the anchor is present exactly once."""
        text = self.read(rel)
        self.assertEqual(text.count(old), 1, f"anchor {old!r} must occur exactly once in {rel}")
        self.write(rel, text.replace(old, new))

    def remove(self, rel: str) -> None:
        (self.root / rel).unlink()
        self.corpus = Corpus(self.cfg)

    def set_age(self, rel: str, days: float) -> None:
        stamp = time.time() - days * 86400
        os.utime(self.root / rel, (stamp, stamp))

    def init_repo(self) -> None:
        """Turn the corpus copy into a git repo with one committed baseline."""
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "ctxkit-tests")
        git(self.root, "config", "user.email", "tests@localhost")
        git(self.root, "config", "commit.gpgsign", "false")
        # Background `git maintenance` can create .git/maintenance.lock mid-test,
        # which raced a test deleting .git on CI. No auto housekeeping in fixtures.
        git(self.root, "config", "maintenance.auto", "false")
        git(self.root, "config", "gc.auto", "0")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "baseline corpus")

    def commit_all(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def head_message(self) -> str:
        return git(self.root, "log", "-1", "--format=%s").strip()

    def commit_count(self) -> int:
        return int(git(self.root, "rev-list", "--count", "HEAD").strip())
