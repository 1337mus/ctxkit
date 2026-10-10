"""Builders for synthetic Claude Code and Codex transcripts, in the same JSONL shapes the tools write."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import support


def claude_text(text: str = "ok") -> str:
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


def claude_tool(name: str, **tool_input: object) -> str:
    item = {"type": "tool_use", "id": "toolu_x", "name": name, "input": tool_input}
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [item]}})


def claude_result(text: str = "result") -> str:
    item = {"type": "tool_result", "tool_use_id": "toolu_x", "content": text}
    return json.dumps({"type": "user", "message": {"role": "user", "content": [item]}})


def codex_message(text: str = "ok", role: str = "assistant") -> str:
    kind = "output_text" if role == "assistant" else "input_text"
    return json.dumps({"timestamp": "t", "type": "response_item",
                       "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}})


def codex_call(command: str, field: str = "arguments", name: str = "exec_command", kind: str = "function_call") -> str:
    """A Codex tool call. ``field`` selects whether the text sits under ``arguments`` or ``input``."""
    value: object = json.dumps({"cmd": command}) if kind == "function_call" else command
    return json.dumps({"timestamp": "t", "type": "response_item",
                       "payload": {"type": kind, "name": name, field: value, "call_id": "call_x"}})


def codex_patch_end(*paths: str, success: bool = True) -> str:
    changes = {p: {"type": "update", "unified_diff": "@@"} for p in paths}
    return json.dumps({"timestamp": "t", "type": "event_msg",
                       "payload": {"type": "patch_apply_end", "call_id": "call_x", "success": success,
                                   "changes": changes}})


class TranscriptCase(support.WorkspaceCase):
    """WorkspaceCase plus helpers to drop transcripts where the configured globs will find them."""

    outside = "/work/shop-api"

    def subs(self) -> dict[str, str]:
        return {"{ROOT}": str(self.root), "{OUTSIDE}": self.outside, "{HOME}": str(self.home)}

    def fixture_lines(self, name: str) -> list[str]:
        text = (support.FIXTURES / "transcripts" / name).read_text()
        for key, value in self.subs().items():
            text = text.replace(key, value)
        return [line for line in text.splitlines() if line.strip()]

    def _place(self, path: Path, lines: list[str], age_days: float) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def add_claude(self, name: str, lines: list[str], age_days: float = 0) -> Path:
        return self._place(self.claude_dir / "project-a" / f"{name}.jsonl", lines, age_days)

    def add_codex(self, name: str, lines: list[str], age_days: float = 0) -> Path:
        return self._place(self.codex_dir / "2026" / "03" / "02" / f"rollout-{name}.jsonl", lines, age_days)

    def root_path(self, rel: str) -> str:
        return str(self.root / rel)
