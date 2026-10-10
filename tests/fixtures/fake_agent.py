#!/usr/bin/env python3
"""A stand-in for a coding agent, used by the recall tests.

It receives the quiz prompt as argv[1], finds the knowledge base root and the clue
words in that prompt, searches the markdown there for a line containing every clue,
and answers with the ticket-style key on that line. Behaviour is steered by
environment variables so tests can simulate good and bad agents:

  FAKE_AGENT_MODE
    plain        (default) a short reply with the key on the last line
    claude_json  one JSON object with result and usage, like `claude -p --output-format json`
    codex_text   the key alone on stdout and "tokens used" on stderr, like `codex exec`
    formatted    the key wrapped in markdown emphasis and a trailing full stop
    none         answers NONE
    wrong        answers a key that does not exist
    sleep        hangs, to exercise the timeout
    fail         exits with status 3
    write        creates a file in the knowledge base, then answers plainly
  FAKE_AGENT_SABOTAGE   if this text appears in the prompt, answer NONE for that question
  FAKE_AGENT_DUMP       if set, write the prompt and working directory to this path
"""

import json
import os
import re
import sys
import time
from pathlib import Path

KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


def find_answer(root: Path, clues: list[str]) -> str:
    for path in sorted(root.rglob("*.md")):
        for line in path.read_text(errors="ignore").splitlines():
            lowered = line.lower()
            if all(clue in lowered for clue in clues):
                match = KEY.search(line)
                if match:
                    return match.group(0)
    return "NONE"


def main() -> int:
    prompt = sys.argv[1]
    dump = os.environ.get("FAKE_AGENT_DUMP")
    if dump:
        Path(dump).write_text(json.dumps({"prompt": prompt, "cwd": os.getcwd()}))
    mode = os.environ.get("FAKE_AGENT_MODE", "plain")

    root_match = re.search(r"knowledge base at (\S+?)\.\n", prompt)
    clue_line = next((l for l in prompt.splitlines() if l.startswith("Clues: ")), "Clues: ")
    clues = [c.strip().lower() for c in clue_line[len("Clues: "):].split(",") if c.strip()]
    root = Path(root_match.group(1)) if root_match else Path.cwd()

    if mode == "sleep":
        time.sleep(30)
        return 0
    if mode == "fail":
        print("agent crashed on purpose", file=sys.stderr)
        return 3
    if mode == "write":
        (root / "injected-by-agent.md").write_text("# Oops\n")

    sabotage = os.environ.get("FAKE_AGENT_SABOTAGE")
    if mode == "none" or (sabotage and sabotage in prompt):
        answer = "NONE"
    elif mode == "wrong":
        answer = "ZZZ-999"
    else:
        answer = find_answer(root, clues)

    if mode == "claude_json":
        usage = {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 30}
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": f"Found it in the index.\n{answer}", "usage": usage}))
    elif mode == "codex_text":
        print(answer)
        print("tokens used\n1,234", file=sys.stderr)
    elif mode == "formatted":
        print(f"I searched the knowledge base.\n\n**`{answer}`**.")
    else:
        print(f"I started at README.md and followed the links.\n{answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
