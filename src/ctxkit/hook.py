"""``ctx hook stop``: a rolling capture checkpoint, run when an agent finishes a turn.

The hook counts work that has not been written down yet and, past a threshold,
blocks the stop once with a message asking the agent to record durable context.
It never injects context, and it always exits 0: any exception prints ``{}``.

Two independent triggers, because they catch different sessions:

* **Edits.** File edits outside the corpus. A coding session produces these.
* **Milestones.** Durable git and gh actions: commit, push, merge, rebase,
  cherry-pick, revert, ``gh pr create|merge|edit|ready|review|close``, and
  ``gh api ... -X POST|PATCH|PUT|DELETE``. An orchestration or review session can
  make dozens of these with almost no edits, and they are where merge states and
  decisions come from. Read-only polling is deliberately not counted.

Any write under the corpus root counts as a capture and resets both counters.

Bookkeeping uses cumulative totals. The marker file stores the totals at the last
nudge, ``{"e": N, "m": N}``. Work still unrecorded is the total minus whichever is
larger, the total at the last capture or the total at the last nudge. That gives
the rolling cadence (8, 16, 24 edits) and a clean restart after a capture, with
no state that can go stale when the counter drops.
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ensure_state_dir
from .transcripts import RootMatcher, iter_events

MILESTONE_RE = re.compile(
    r"gh pr (?:create|merge|edit|ready|review|close)(?![\w-])"
    r"|git(?:\s+(?:-C|-c)\s+\S+)*\s+(?:commit|push|merge|rebase|cherry-pick|revert)(?![\w-])"
    r"|gh api(?![\w-])[^|;&\n]*?-(?:X\s*|-method[\s=]+)(?:POST|PATCH|PUT|DELETE)(?![\w-])"
)

SESSION_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId", "id")
TRANSCRIPT_KEYS = ("transcript_path", "transcriptPath", "rollout_path", "rolloutPath",
                   "session_file", "sessionFile", "transcript")


def milestone_count(command: str) -> int:
    """Durable-state git or gh actions in one shell command."""
    return len(MILESTONE_RE.findall(command))


@dataclass
class Work:
    """Cumulative counts over a transcript, plus their values at the most recent capture."""

    edits_total: int = 0
    edits_at_capture: int = 0
    miles_total: int = 0
    miles_at_capture: int = 0

    @property
    def edits_since_capture(self) -> int:
        return self.edits_total - self.edits_at_capture

    @property
    def miles_since_capture(self) -> int:
        return self.miles_total - self.miles_at_capture


def compute_work(transcript: str | os.PathLike[str], matcher: RootMatcher) -> Work:
    """Walk a transcript in order, noting totals and where the last corpus write happened."""
    work = Work()
    for event in iter_events(transcript, matcher):
        if event.kind == "edit":
            work.edits_total += 1
        elif event.kind == "command":
            work.miles_total += milestone_count(event.value)
        elif event.kind == "write":
            work.edits_at_capture = work.edits_total
            work.miles_at_capture = work.miles_total
    return work


def read_marker(path: Path) -> tuple[int, int]:
    """(edits, milestones) totals recorded at the last nudge. Anything unreadable means never nudged."""
    try:
        data = json.loads(path.read_text())
        return max(0, int(data.get("e") or 0)), max(0, int(data.get("m") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0


def first_key(payload: dict[str, object], keys: tuple[str, ...]) -> str | None:
    """The first non-empty string among ``keys``. Harnesses spell these fields differently."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def newest_codex_rollout(cfg: Config) -> str | None:
    """Last resort when a Codex payload names no transcript. Racy with concurrent sessions."""
    try:
        paths = glob.glob(cfg.codex_glob, recursive=True)
        return max(paths, key=lambda p: os.stat(p).st_mtime) if paths else None
    except (OSError, ValueError):
        return None


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[-120:]


def _remember_payload_shape(cfg: Config, tool: str, raw: str) -> None:
    """Record which fields the harness sent, never their values, to debug a changed contract."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        ensure_state_dir(cfg)
        shape = {key: type(value).__name__ for key, value in data.items()}
        (cfg.state_dir / f"last-hook-payload-{tool}.json").write_text(json.dumps(shape, indent=1, sort_keys=True))
    except (OSError, ValueError):
        pass


def stop_message(cfg: Config) -> str:
    standing = str(cfg.root / cfg.standing_file) if cfg.standing_file else "the standing rules file"
    return cfg.stop_message.replace("{root}", str(cfg.root)).replace("{standing_file}", standing)


def dialect_response(tool: str, message: str) -> dict[str, object]:
    """The block-the-stop answer in the dialect each harness expects."""
    if tool == "codex":
        return {"continue": False, "stopReason": message}
    return {"decision": "block", "reason": message}


def evaluate(cfg: Config, tool: str, raw: str) -> dict[str, object]:
    """Decide whether to block this stop. Returns the JSON object to print. May raise."""
    if tool not in ("claude", "codex"):
        return {}
    _remember_payload_shape(cfg, tool, raw)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return {}
    session_id = first_key(payload, SESSION_KEYS)
    transcript = first_key(payload, TRANSCRIPT_KEYS)
    if not transcript and tool == "codex":
        transcript = newest_codex_rollout(cfg)
    if not session_id and transcript:
        session_id = transcript
    if payload.get("stop_hook_active") or not session_id or not transcript:
        return {}

    ensure_state_dir(cfg)
    if (cfg.state_dir / "disabled").exists():
        return {}

    work = compute_work(transcript, RootMatcher(cfg.root))
    marker = cfg.state_dir / f"nudged-{_safe_name(session_id)}"
    last_e, last_m = read_marker(marker)
    # A marker above the transcript's own total cannot be ours (the file was replaced), so ignore it.
    last_e = last_e if last_e <= work.edits_total else 0
    last_m = last_m if last_m <= work.miles_total else 0

    pending_edits = work.edits_total - max(work.edits_at_capture, last_e)
    pending_miles = work.miles_total - max(work.miles_at_capture, last_m)
    if pending_edits < cfg.nudge_min_edits and pending_miles < cfg.nudge_min_milestones:
        return {}

    # Persist before answering. If the marker cannot be written, stay silent rather than
    # block again on every following turn.
    marker.write_text(json.dumps({"e": work.edits_total, "m": work.miles_total}))
    return dialect_response(tool, stop_message(cfg))


def handle_stop(cfg: Config, tool: str, raw: str) -> dict[str, object]:
    """Fail-open wrapper: any exception becomes an empty answer."""
    try:
        return evaluate(cfg, tool, raw)
    except Exception:
        return {}
