"""Tests for `ctx init`."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from ctxkit import doctor, init
from ctxkit.config import load_config


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "kb"
        cfg_file = Path(self.tmp.name) / "c.toml"
        cfg_file.write_text(f'root = "{self.root}"\nstate_dir = "{self.tmp.name}/state"\n')
        self.cfg = load_config(str(cfg_file))

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_corpus_passes_doctor_and_is_committed(self):
        """Regression: the one-command quickstart must leave a corpus the gate accepts,
        with a commit, or the loss check has no baseline and doctor fails on day one."""
        init.run(self.cfg)
        self.assertEqual(doctor.run(self.cfg), 0)
        log = subprocess.run(["git", "-C", str(self.root), "log", "--oneline"],
                             capture_output=True, text=True).stdout
        self.assertEqual(len(log.splitlines()), 1)

    def test_rerun_never_overwrites_existing_notes(self):
        """Regression: init on an existing corpus must not clobber what is there."""
        self.root.mkdir(parents=True)
        (self.root / "README.md").write_text("my own index\n")
        init.run(self.cfg)
        self.assertEqual((self.root / "README.md").read_text(), "my own index\n")
