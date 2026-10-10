"""Read Claude Code and Codex session transcripts as one stream of events.

Both tools write JSON lines. The parser sniffs the shape of each line instead of
trusting which tool a file belongs to, so a harness that hands the hook either
shape still works. Getting this wrong is silent: the hook would print a valid
answer and never fire.

Claude Code lines:
    ``{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": ..., "input": {...}}]}}``
    Reads are ``Read`` tool calls. Edits are ``Write``, ``Edit``, ``MultiEdit`` and
    ``NotebookEdit`` calls. Shell commands are ``Bash`` calls.

Codex rollout lines:
    ``{"timestamp": ..., "type": "response_item", "payload": {...}}`` where the payload is
    an assistant ``message``, a ``custom_tool_call`` (free-form ``input`` string, for
    example an apply_patch body), or a ``function_call`` (``arguments`` string, usually JSON
    carrying a shell command). Codex versions differ on whether the text sits under
    ``input`` or ``arguments``, so both are always read.
    ``{"type": "event_msg", "payload": {"type": "patch_apply_end", "changes": {path: ...}}}``
    is the authoritative record of files a patch wrote, one key per file.

Codex has no structured read tool, so reads are inferred from command text: any
path under the corpus root that appears in a call and is not being written. That
is a heuristic, and README documents what it cannot see.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, NamedTuple

from .config import Config

CLAUDE_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

_INTERESTING = re.compile(r'"assistant"|tool_use|response_item|patch_apply_end')
_PATCH_HEADER = re.compile(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", re.MULTILINE)
_REDIRECT_BEFORE = re.compile(r"(?:>>?|\btee(?:\s+-a)?)\s*[\"']?$")
_MUTATING = re.compile(r"(?:^|[\s;&|(])(?:sed\s+-[A-Za-z]*i|perl\s+-[A-Za-z]*i|rm|mv|touch|tee|truncate)\b")
_PATH_TAIL = r"(/[^\s'\"`<>|;&()\\*?\[\]{}$,]+)"


class Event(NamedTuple):
    """One thing a transcript says happened.

    kind is one of:
      turn     an assistant message (value is empty)
      read     a corpus file was read (value is its path relative to the root)
      write    a corpus file was written
      edit     a file outside the corpus was edited (value is the path)
      command  a shell command ran (value is its text)
    """

    kind: str
    value: str


class RootMatcher:
    """Recognise paths under the corpus root in structured fields and in free text."""

    def __init__(self, root: Path, home: Path | None = None) -> None:
        self.home = home or Path.home()
        roots = {os.path.normpath(str(root)), os.path.normpath(str(root.resolve()))}
        self.roots = sorted(roots)
        alternatives: list[str] = []
        home_text = os.path.normpath(str(self.home))
        for r in self.roots:
            alternatives.append(re.escape(r))
            if r.startswith(home_text + os.sep):
                tail = r[len(home_text):]
                alternatives += [re.escape("~" + tail), re.escape("$HOME" + tail), re.escape("${HOME}" + tail)]
        self._text_re = re.compile(r"(?<![\w./~$-])(?:" + "|".join(alternatives) + ")" + _PATH_TAIL)
        self._ignored_edit_prefixes = (
            str(self.home / ".claude") + os.sep,
            str(self.home / ".codex") + os.sep,
        )

    def rel(self, raw: str) -> str | None:
        """Relative posix path when ``raw`` names a file under the root, else None."""
        path = raw
        if path.startswith("~"):
            path = str(self.home) + path[1:] if path.startswith("~/") else os.path.expanduser(path)
        if not os.path.isabs(path):
            return None
        path = os.path.normpath(path)
        for r in self.roots:
            if path.startswith(r + os.sep):
                return path[len(r) + 1:].replace(os.sep, "/")
        return None

    def is_harness_path(self, raw: str) -> bool:
        """Files inside the agent harness's own config directories are not project work."""
        return os.path.normpath(raw).startswith(self._ignored_edit_prefixes)

    def spans(self, text: str) -> list[tuple[str, int, int]]:
        """Corpus files mentioned in ``text`` as (relative path, start, end). Requires a file extension."""
        found = []
        for match in self._text_re.finditer(text):
            whole = match.group(0).rstrip(".")
            if whole.startswith("${HOME}"):
                whole = str(self.home) + whole[len("${HOME}"):]
            elif whole.startswith("$HOME"):
                whole = str(self.home) + whole[len("$HOME"):]
            rel = self.rel(whole)
            if rel is None or not re.search(r"\.[A-Za-z0-9]{1,8}$", rel.rsplit("/", 1)[-1]):
                continue
            found.append((rel, match.start(), match.end()))
        return found


def _flatten(value: object, parts: list[str]) -> None:
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped[0] in "{[":
            try:
                decoded = json.loads(stripped)
            except ValueError:
                parts.append(value)
                return
            _flatten(decoded, parts)
            return
        parts.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _flatten(item, parts)
    elif isinstance(value, list):
        for item in value:
            _flatten(item, parts)


def payload_text(payload: dict[str, object]) -> str:
    """All text a Codex tool call carries.

    Reads ``input`` and ``arguments`` (and ``action`` for older shell-call records)
    because versions differ on which one holds the text. A JSON-encoded string is
    decoded so shell commands appear as plain text.
    """
    parts: list[str] = []
    for key in ("input", "arguments", "action"):
        _flatten(payload.get(key), parts)
    return "\n".join(parts)


def classify_paths(text: str, matcher: RootMatcher) -> tuple[list[str], list[str]]:
    """Split corpus paths mentioned in a Codex call into (reads, writes).

    * An apply_patch body: the ``*** Add/Update/Delete File:`` headers are writes and
      nothing else in the patch counts, because patch content only quotes text.
    * A path directly after ``>``, ``>>`` or ``tee`` is a write.
    * Any path in a command that also uses sed -i, perl -i, rm, mv, touch, tee or
      truncate is treated as a write.
    * Everything else is a read.
    """
    if "*** Begin Patch" in text:
        writes = []
        for header in _PATCH_HEADER.findall(text):
            rel = matcher.rel(header.strip())
            if rel is not None and rel not in writes:
                writes.append(rel)
        return [], writes
    mutating = bool(_MUTATING.search(text))
    reads: list[str] = []
    writes: list[str] = []
    for rel, start, _end in matcher.spans(text):
        before = text[max(0, start - 14):start]
        bucket = writes if (mutating or _REDIRECT_BEFORE.search(before)) else reads
        if rel not in bucket:
            bucket.append(rel)
    return reads, writes


def _is_shell_tool(name: object, kind: str) -> bool:
    if kind == "local_shell_call":
        return True
    lowered = str(name or "").lower()
    return any(word in lowered for word in ("shell", "exec", "bash", "command"))


def _claude_events(obj: dict[str, object], matcher: RootMatcher) -> Iterator[Event]:
    if obj.get("type") != "assistant":
        return
    yield Event("turn", "")
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        name = item.get("name")
        raw_input = item.get("input")
        data = raw_input if isinstance(raw_input, dict) else {}
        if name == "Read":
            target = data.get("file_path")
            rel = matcher.rel(target) if isinstance(target, str) else None
            if rel is not None:
                yield Event("read", rel)
        elif name in CLAUDE_EDIT_TOOLS:
            target = data.get("file_path") or data.get("notebook_path")
            if not isinstance(target, str):
                continue
            rel = matcher.rel(target)
            if rel is not None:
                yield Event("write", rel)
            elif not matcher.is_harness_path(target):
                yield Event("edit", target)
        elif name == "Bash":
            command = data.get("command")
            if isinstance(command, str):
                yield Event("command", command)


def _codex_events(obj: dict[str, object], matcher: RootMatcher) -> Iterator[Event]:
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return
    top = obj.get("type")
    if top == "response_item":
        kind = payload.get("type")
        if kind == "message" and payload.get("role") == "assistant":
            yield Event("turn", "")
        elif kind in ("custom_tool_call", "function_call", "local_shell_call"):
            text = payload_text(payload)
            # A patch body only quotes text, so it never counts as a command. Anything
            # else shaped like a shell call does, whether the version names its text
            # field "arguments" or "input".
            if "*** Begin Patch" not in text and _is_shell_tool(payload.get("name"), str(kind)):
                yield Event("command", text)
            reads, writes = classify_paths(text, matcher)
            for rel in reads:
                yield Event("read", rel)
            for rel in writes:
                yield Event("write", rel)
    elif top == "event_msg" and payload.get("type") == "patch_apply_end":
        if payload.get("success") is False:
            return
        changes = payload.get("changes")
        if not isinstance(changes, dict):
            return
        for target in changes:
            if not isinstance(target, str):
                continue
            rel = matcher.rel(target)
            if rel is not None:
                yield Event("write", rel)
            elif not matcher.is_harness_path(target):
                yield Event("edit", target)


def iter_events(path: str | os.PathLike[str], matcher: RootMatcher) -> Iterator[Event]:
    """Yield the events of one transcript file in order. Unreadable files and bad lines are skipped."""
    try:
        handle = open(path, "r", errors="ignore")
    except OSError:
        return
    with handle:
        for line in handle:
            if not _INTERESTING.search(line):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            yield from _claude_events(obj, matcher)
            yield from _codex_events(obj, matcher)


@dataclass
class Session:
    """What one transcript did, summarised."""

    tool: str
    path: str
    mtime: float
    turns: int = 0
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)


def transcript_files(cfg: Config) -> list[tuple[str, str]]:
    """(tool, path) for every transcript matched by an enabled source."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tool, enabled, pattern in (
        ("claude", cfg.claude_enabled, cfg.claude_glob),
        ("codex", cfg.codex_enabled, cfg.codex_glob),
    ):
        if not enabled:
            continue
        for path in sorted(glob.glob(pattern, recursive=True)):
            if path not in seen and os.path.isfile(path):
                seen.add(path)
                found.append((tool, path))
    return found


def read_session(tool: str, path: str, matcher: RootMatcher) -> Session:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    session = Session(tool=tool, path=path, mtime=mtime)
    for event in iter_events(path, matcher):
        if event.kind == "turn":
            session.turns += 1
        elif event.kind == "read":
            session.reads.add(event.value)
        elif event.kind == "write":
            session.writes.add(event.value)
    return session


def scan_sessions(cfg: Config, days: float, now: float | None = None) -> list[Session]:
    """Sessions from every enabled source whose transcript changed within ``days``."""
    cutoff = (time.time() if now is None else now) - days * 86400
    matcher = RootMatcher(cfg.root)
    sessions = []
    for tool, path in transcript_files(cfg):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        sessions.append(read_session(tool, path, matcher))
    return sessions
