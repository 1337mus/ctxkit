"""``ctx snapshot``: commit the corpus when it has uncommitted changes.

Committing must not depend on an agent remembering to do it. A corpus that goes
weeks without a commit leaves the loss detector comparing against a stale
baseline, so the check quietly stops protecting anything.

The snapshot refuses to commit while content loss exists, because committing
would make the loss the new baseline. Commits are unsigned by default: an
unattended job cannot answer a graphical passphrase prompt and would hang.
"""

from __future__ import annotations

import subprocess
import sys

from .config import Config
from .corpus import Corpus
from .doctor import check_loss, run_git

FALLBACK_NAME = "ctxkit"
FALLBACK_EMAIL = "ctxkit@localhost"


def _identity_args(corpus: Corpus) -> list[str]:
    """Provide a committer identity only when git has none, so unattended jobs still work."""
    proc = run_git(corpus.root, "config", "user.email", timeout=15)
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return []
    return ["-c", f"user.name={FALLBACK_NAME}", "-c", f"user.email={FALLBACK_EMAIL}"]


def run(cfg: Config) -> int:
    """Entry point. Returns 0 when nothing needed doing or the commit succeeded, 1 otherwise."""
    if not cfg.snapshot_enabled:
        print("snapshot: disabled in config ([snapshot] enabled = false)")
        return 0
    corpus = Corpus(cfg)
    if not corpus.exists:
        print(f"snapshot: corpus root {cfg.root} does not exist", file=sys.stderr)
        return 1
    if not corpus.is_git_repo:
        print("snapshot: corpus is not a git repo, nothing to do")
        return 0

    status = run_git(corpus.root, "status", "--porcelain", timeout=30)
    if status is None or status.returncode != 0:
        print("snapshot: git status failed", file=sys.stderr)
        return 1
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if not changed:
        return 0

    loss = check_loss(corpus)
    if loss.lost:
        print(f"snapshot: REFUSED, {len(loss.lost)} item(s) lost since the last commit: {', '.join(loss.lost[:8])}")
        print("  Restore them, or commit the removal by hand with a message saying it was deliberate.")
        return 1

    message = f"snapshot: {len(changed)} file(s) changed"
    global_opts = _identity_args(corpus)
    commit_cmd = ["commit", "-q", "-m", message]
    if cfg.snapshot_sign:
        commit_cmd.insert(1, "-S")
    else:
        global_opts += ["-c", "commit.gpgsign=false"]
    try:
        add = run_git(corpus.root, "add", "-A", timeout=60)
        if add is None or add.returncode != 0:
            raise subprocess.SubprocessError(add.stderr.strip() if add else "git add failed")
        commit = run_git(corpus.root, *global_opts, *commit_cmd, timeout=60)
        if commit is None or commit.returncode != 0:
            raise subprocess.SubprocessError(commit.stderr.strip() if commit else "git commit failed")
    except subprocess.SubprocessError as exc:
        print(f"snapshot: commit failed: {exc}", file=sys.stderr)
        return 1
    print(f"snapshot: committed {len(changed)} file(s)")
    return 0
