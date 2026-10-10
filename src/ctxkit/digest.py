"""``ctx digest``: a one-page markdown report of the system's own health.

A scorecard that only answers when asked arrives too late, so the digest is meant
to be produced on a schedule and to lead with what is wrong. ``## Tripped`` comes
first and lists every condition that crossed a limit. Below it come the compliance
trend with a week-over-week delta, the structure of the corpus, and the friction
agents hit from the gate.

The friction counts are keyword mentions in transcripts. They include sessions that
were merely discussing a finding, so read them as a rough signal, not a measurement.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import Counter
from datetime import date, timedelta

from .config import Config, ensure_state_dir
from .corpus import Corpus
from .doctor import HEADINGS, Findings, has_head, run_checks, run_git
from .stats import read_trend
from .transcripts import transcript_files

STALE_COMMIT_SECONDS = 2 * 86400
FRICTION_DAYS = 7


def tripped_conditions(findings: Findings, cfg: Config, corpus: Corpus, now: float) -> list[str]:
    """Every condition that has crossed its limit, in a stable order."""
    tripped: list[str] = []
    ri = findings.root_index
    if ri.over_tokens:
        tripped.append(f"Root index is about {ri.tokens:,} tokens, over the {ri.token_limit:,} limit")
    if ri.over_entries:
        tripped.append(f"{len(ri.over_entries)} root index entr{'y' if len(ri.over_entries) == 1 else 'ies'} "
                       f"over {cfg.index_entry_max_bytes} bytes")
    if findings.loss.lost:
        tripped.append(f"{len(findings.loss.lost)} identifier or rule item(s) lost since the last commit")
    if findings.orphans:
        tripped.append(f"{len(findings.orphans)} file(s) unreachable from the root index")
    if findings.fat_regrown:
        tripped.append(f"{len(findings.fat_regrown)} dormant regrown file(s) awaiting a split")
    if findings.expired:
        tripped.append(f"{len(findings.expired)} verify-by marker(s) past due")
    if corpus.is_git_repo and has_head(cfg.root):
        proc = run_git(cfg.root, "log", "-1", "--format=%ct", timeout=15)
        try:
            age = now - int(proc.stdout.strip()) if proc and proc.returncode == 0 else 0
        except ValueError:
            age = 0
        if age >= STALE_COMMIT_SECONDS:
            tripped.append(f"No corpus commit for {int(age // 86400)} days")
    return tripped


def friction_counts(cfg: Config, now: float) -> Counter[str]:
    """Mentions of each doctor heading in recent transcripts, streamed so large files stay cheap."""
    counts: Counter[str] = Counter()
    cutoff = now - FRICTION_DAYS * 86400
    for _tool, path in transcript_files(cfg):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            with open(path, errors="ignore") as handle:
                for line in handle:
                    for heading in HEADINGS:
                        if heading in line:
                            counts[heading] += line.count(heading)
        except OSError:
            continue
    return counts


def build_digest(cfg: Config, today: date | None = None, now: float | None = None) -> tuple[str, list[str], str]:
    """Return (markdown, tripped conditions, one-line summary)."""
    now = time.time() if now is None else now
    today = today or date.fromtimestamp(now)
    corpus = Corpus(cfg)
    findings = run_checks(corpus, today=today, now=now)
    tripped = tripped_conditions(findings, cfg, corpus, now)

    lines = [f"# Context digest, week of {today.isoformat()}", "", "## Tripped"]
    lines += [f"- **{item}**" for item in tripped] or ["- Nothing tripped."]
    lines.append("")

    rows = read_trend(cfg)
    lines.append("## Are agents using the corpus?")
    if rows:
        current = rows[-1]
        earlier = next((r for r in reversed(rows) if r.date <= (date.fromisoformat(current.date) - timedelta(days=7)).isoformat()), None)
        delta = f", {current.pct - earlier.pct:+.1f} vs a week ago" if earlier else ""
        lines.append(f"- Pull compliance: **{current.pct:.1f}%** of {current.substantial} substantial sessions "
                     f"({cfg.trend_window_days}d window{delta})")
    else:
        lines.append("- No trend yet. Run `ctx stats`, or schedule it, to start one.")
    lines.append("")

    files = corpus.files()
    total_bytes = sum(p.stat().st_size for p in files if p.exists())
    archived = sum(1 for p in files if corpus.is_archived(p))
    ri = findings.root_index
    gate = []
    if findings.orphans:
        gate.append(f"{len(findings.orphans)} unreachable")
    if findings.loss.lost:
        gate.append(f"{len(findings.loss.lost)} lost")
    if findings.fat_regrown:
        gate.append(f"{len(findings.fat_regrown)} dormant regrown")
    lines += [
        "## Structure",
        f"- Corpus: **{len(files)} files, {total_bytes // 1024} KB** ({archived} archived)",
        f"- Root index: **{ri.size_bytes // 1024} KB** (~{ri.tokens:,} tokens, limit {ri.token_limit:,})",
        f"- Gate: {'**' + ', '.join(gate) + '**' if gate else 'clean'}, "
        f"{len(findings.fat_first)} file(s) over {cfg.fat_file_bytes // 1000} KB awaiting a split",
        "",
        f"## Gate friction, last {FRICTION_DAYS} days (mentions in agent transcripts)",
    ]
    counts = friction_counts(cfg, now)
    lines += [f"- {heading}: {n}" for heading, n in counts.most_common() if n] or ["- none"]
    lines.append("- These counts include sessions that discussed a finding without hitting it.")
    lines.append("")

    compliance = f"{rows[-1].pct:.1f}%" if rows else "unknown"
    summary = (f"{len(tripped)} condition(s) tripped, compliance {compliance}" if tripped
               else f"nothing tripped, compliance {compliance}")
    return "\n".join(lines), tripped, summary


def _notify(cfg: Config, path: str, summary: str) -> None:
    if not cfg.notify_command:
        print("digest: --notify given but [digest] notify_command is not configured", file=sys.stderr)
        return
    argv = [part.replace("{path}", path).replace("{summary}", summary) for part in cfg.notify_command]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"digest: notify command failed: {exc}", file=sys.stderr)
        return
    if proc.returncode != 0:
        print(f"digest: notify command exited {proc.returncode}", file=sys.stderr)


def run(cfg: Config, notify: bool = False, today: date | None = None, now: float | None = None) -> int:
    """Entry point for ``ctx digest``."""
    corpus = Corpus(cfg)
    if not corpus.exists:
        print(f"ERROR: corpus root {cfg.root} does not exist", file=sys.stderr)
        return 1
    now = time.time() if now is None else now
    today = today or date.fromtimestamp(now)
    text, _tripped, summary = build_digest(cfg, today, now)
    day = today.isoformat()
    try:
        ensure_state_dir(cfg)
        cfg.digest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        out = cfg.digest_dir / f"{day}.md"
        out.write_text(text)
        (cfg.digest_dir / "latest.md").write_text(text)
    except OSError as exc:
        print(f"digest failed: {exc}", file=sys.stderr)
        return 1
    print(text)
    print(f"\nwritten: {out}")
    if notify:
        _notify(cfg, str(out), summary)
    return 0
