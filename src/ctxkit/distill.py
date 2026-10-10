"""``ctx distill``: a brief for a promotion pass, meant to be read by an agent.

Promotion is judgement work: deciding which lessons hold beyond one ticket, which
rule already covers a new lesson, and which two files now say the same thing. The
tool cannot make those calls, so it assembles the evidence and states the task.
"""

from __future__ import annotations

import sys
import time
from datetime import date

from .config import Config
from .corpus import Corpus
from .doctor import fat_candidates
from .stats import compute_stats


def build_brief(cfg: Config, days: int, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    corpus = Corpus(cfg)
    cutoff = now - days * 86400
    lines = [f"=== ctx distill (last {days} days) ===", "", f"FILES MODIFIED (last {days}d):"]

    recent = []
    for path in corpus.files():
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            recent.append((st.st_mtime, st.st_size, corpus.rel(path)))
    recent.sort(reverse=True)
    lines += [f"  {rel} ({size / 1024:.1f} KB, modified {date.fromtimestamp(mtime).isoformat()})"
              for mtime, size, rel in recent] or ["  (none)"]

    fat = fat_candidates(corpus)
    lines += ["", f"SPLIT OR DISTILL CANDIDATES (over {cfg.fat_file_bytes // 1000} KB, README and {cfg.archive_dirname}/ exempt):"]
    lines += [f"  {f.path} ({f.size / 1024:.1f} KB)" for f in fat] or ["  (none)"]

    lines.append("")
    if not cfg.standing_files:
        lines.append("PROMOTION TARGET: none configured (set standing_files)")
    for index, name in enumerate(cfg.standing_files):
        target = cfg.root / name
        label = "PROMOTION TARGET" if index == 0 else "COMPANION STANDING FILE"
        if target.is_file():
            note = "generalized lessons land here" if index == 0 else "split target when the first file grows unwieldy"
            lines.append(f"{label}: {name} ({target.stat().st_size / 1024:.1f} KB), {note}")
        else:
            lines.append(f"{label}: {name} (missing)")

    dead = compute_stats(cfg, cfg.trend_window_days, now).dead
    lines += ["", f"PRUNE CANDIDATES (read by no session in {cfg.trend_window_days}d, all tools; excludes README.md, "
                  f"{cfg.archive_dirname}/, excluded folders and files newer than {cfg.dead_weight_min_age_days}d): {len(dead)}"]
    lines += [f"  {rel}" for rel in dead[:20]]
    if len(dead) > 20:
        lines.append(f"  (+{len(dead) - 20} more not shown)")

    target_name = cfg.standing_file or "the standing rules file"
    lines += [
        "",
        "INSTRUCTIONS:",
        "  Read the modified files above. Keep only lessons that hold beyond a single ticket or project.",
        f"  Promote each as one short imperative rule in {target_name}, and strengthen an existing rule",
        "  instead of adding a near duplicate. Leave project specifics in their topic file.",
        "  Split oversized files into a companion and index both. Never trim a file to fit a size number.",
        "  Decide whether prune candidates are finished work for the archive, still needed, or safe to merge.",
        "  Check whether any two listed files now cover the same topic and should be merged. That is a",
        "  judgement call to make while reading, not a mechanical check.",
        "  Finish with `ctx doctor` and fix anything it reports.",
    ]
    return lines


def run(cfg: Config, days: int, now: float | None = None) -> int:
    corpus = Corpus(cfg)
    if not corpus.exists:
        print(f"ERROR: corpus root {cfg.root} does not exist", file=sys.stderr)
        return 1
    for line in build_brief(cfg, days, now):
        print(line)
    return 0
