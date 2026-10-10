"""`ctx init`: create a starter corpus in one step.

Creates the corpus folder, a routing README, the first standing-rules file and a
git repository with one commit, then prints the two things the user still has to
paste: the pull instruction for their agent and the stop hook. Never overwrites
an existing file, so it is safe to run on a corpus that already exists.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

from .config import Config
from .snapshot import FALLBACK_EMAIL, FALLBACK_NAME

AGENT_SNIPPET = """\
## Knowledge base

Before non-trivial work, read {root}/README.md and open the files it points to.
Search the folder with grep before guessing. When you learn something durable,
such as a decision, a gotcha or a deploy state, write it into the matching file.
Keep ticket keys and dates. Never delete a fact to make a file shorter; move it
to a companion file instead. Run `ctx doctor` before you finish."""

HOOK_SNIPPET = """\
{
  "hooks": {
    "Stop": [ { "hooks": [ { "type": "command", "command": "ctx hook stop" } ] } ]
  }
}"""


def _starter_files(cfg: Config) -> dict[str, str]:
    today = date.today().isoformat()
    rules = cfg.standing_files[0] if cfg.standing_files else "standing-rules.md"
    return {
        "README.md": (
            f"# Knowledge base\n\nUpdated: {today}\n\n"
            "Start here. This file only points to other files.\n\n"
            f"- [{rules}]({rules}): short rules that apply to every project.\n"
        ),
        rules: (
            f"# Standing rules\n\nUpdated: {today}\n\n"
            "Short rules learned the hard way. Each one opens with a bold sentence.\n"
        ),
    }


def _git(root: Path, *args: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run(cfg: Config) -> int:
    root = cfg.root
    root.mkdir(parents=True, exist_ok=True)
    created = []
    for name, body in _starter_files(cfg).items():
        path = root / name
        if not path.exists():
            path.write_text(body)
            created.append(name)

    if not (root / ".git").exists():
        if not _git(root, "init", "-q"):
            print("ctx init: git is not available; content-loss checks will be skipped")
    if created and (root / ".git").exists():
        identity = []
        if not _git(root, "config", "user.email"):
            identity = ["-c", f"user.name={FALLBACK_NAME}", "-c", f"user.email={FALLBACK_EMAIL}"]
        _git(root, "add", "-A")
        _git(root, *identity, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "Start the knowledge base")

    print(f"Knowledge base ready at {root}")
    print(f"  created: {', '.join(created) if created else 'nothing, it already existed'}\n")
    print("Two last steps:\n")
    print("1. Paste this into the AGENTS.md or CLAUDE.md your agent reads:\n")
    print(AGENT_SNIPPET.format(root=root))
    print("\n2. Optional: add the stop hook to ~/.claude/settings.json (Codex: see docs/integrations.md):\n")
    print(HOOK_SNIPPET)
    print("\nCheck it any time with: ctx doctor")
    return 0
