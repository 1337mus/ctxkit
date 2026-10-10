"""``ctx stats``: how agents actually use the corpus, from their transcripts.

The headline number is pull compliance: of the substantial sessions in the window,
how many read at least one corpus file. It says nothing about whether the read
file was the right one or whether the answer was used. ``ctx recall`` measures
that separately.

Dead weight and index routing use reads from every enabled tool. Claude Code reads
come from the ``Read`` tool. Codex has no read tool, so its reads are inferred from
command text (see ``transcripts``). Counting only one tool makes both measures
collapse the day usage moves to the other.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from .config import Config, ensure_state_dir
from .corpus import Corpus
from .transcripts import Session, scan_sessions

TREND_HEADER = "date\tsubstantial\tpct\tclaude_n\tclaude_pct\tcodex_n\tcodex_pct"


@dataclass
class ToolStats:
    sessions: int = 0
    substantial: int = 0
    compliant: int = 0
    wrote: int = 0

    @property
    def pct(self) -> float:
        return 100.0 * self.compliant / self.substantial if self.substantial else 0.0


@dataclass
class Routing:
    read_index: int = 0
    routed: int = 0

    @property
    def failed(self) -> int:
        return self.read_index - self.routed

    @property
    def success_pct(self) -> float:
        return 100.0 * self.routed / self.read_index if self.read_index else 0.0

    @property
    def failure_pct(self) -> float:
        return 100.0 * self.failed / self.read_index if self.read_index else 0.0


@dataclass
class StatsReport:
    days: int
    per_tool: dict[str, ToolStats]
    combined: ToolStats
    top_read: list[tuple[str, int]]
    dead: list[str]
    index_bytes: int
    index_tokens: int
    index_flagged: bool
    routing: Routing
    sessions: list[Session] = field(default_factory=list, repr=False)


@dataclass
class TrendRow:
    date: str
    substantial: int
    pct: float
    claude_pct: float | None = None
    codex_pct: float | None = None


def compute_stats(cfg: Config, days: int, now: float | None = None) -> StatsReport:
    """Summarise sessions from every enabled source over the last ``days`` days."""
    now = time.time() if now is None else now
    corpus = Corpus(cfg)
    sessions = scan_sessions(cfg, days, now)
    substantial = [s for s in sessions if s.turns >= cfg.substantial_min_turns]

    per_tool = {"claude": ToolStats(), "codex": ToolStats()}
    combined = ToolStats()
    for s in sessions:
        per_tool[s.tool].sessions += 1
        combined.sessions += 1
    for s in substantial:
        for bucket in (per_tool[s.tool], combined):
            bucket.substantial += 1
            bucket.compliant += 1 if s.reads else 0
            bucket.wrote += 1 if s.writes else 0

    reads_per_session: Counter[str] = Counter()
    for s in substantial:
        reads_per_session.update(r for r in s.reads if r.endswith(".md"))

    read_anywhere: set[str] = set()
    for s in sessions:
        read_anywhere |= s.reads
    dead = []
    for path in corpus.files():
        if path.name == "README.md" or corpus.is_excluded(path) or corpus.is_archived(path):
            continue
        age = corpus.age_days(path, now)
        if age is None or age < cfg.dead_weight_min_age_days:
            continue
        if corpus.rel(path) not in read_anywhere:
            dead.append(corpus.rel(path))

    try:
        index_bytes = (cfg.root / "README.md").stat().st_size
    except OSError:
        index_bytes = 0

    routing = Routing()
    for s in substantial:
        if "README.md" in s.reads:
            routing.read_index += 1
            routing.routed += 1 if any(r != "README.md" for r in s.reads) else 0

    return StatsReport(
        days=days,
        per_tool=per_tool,
        combined=combined,
        top_read=reads_per_session.most_common(15),
        dead=sorted(dead),
        index_bytes=index_bytes,
        index_tokens=index_bytes // 4,
        index_flagged=index_bytes // 4 > cfg.index_token_flag,
        routing=routing,
        sessions=sessions,
    )


# ---- trend file -------------------------------------------------------------


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def read_trend(cfg: Config) -> list[TrendRow]:
    """Rows of ``compliance.tsv``. A header, short rows and malformed rows are tolerated."""
    rows: list[TrendRow] = []
    try:
        text = (cfg.state_dir / "compliance.tsv").read_text()
    except OSError:
        return rows
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            datetime.strptime(parts[0], "%Y-%m-%d")
            n, pct = int(parts[1]), float(parts[2])
        except ValueError:
            continue
        rows.append(TrendRow(
            date=parts[0], substantial=n, pct=pct,
            claude_pct=_to_float(parts[4]) if len(parts) > 4 else None,
            codex_pct=_to_float(parts[6]) if len(parts) > 6 else None,
        ))
    return rows


def record_trend(cfg: Config, report: StatsReport, today: date) -> TrendRow | None:
    """Write today's row, replacing an earlier row for the same day. Returns the previous day's row."""
    ensure_state_dir(cfg)
    path = cfg.state_dir / "compliance.tsv"
    iso = today.isoformat()
    kept: list[str] = []
    previous: TrendRow | None = None
    for row in read_trend(cfg):
        if row.date != iso:
            previous = row
    try:
        for line in path.read_text().splitlines():
            if line.strip() and not line.startswith(iso + "\t") and not line.startswith("date\t"):
                kept.append(line)
    except OSError:
        pass
    c, cl, cx = report.combined, report.per_tool["claude"], report.per_tool["codex"]
    new = (f"{iso}\t{c.substantial}\t{c.pct:.1f}\t{cl.substantial}\t{cl.pct:.1f}\t"
           f"{cx.substantial}\t{cx.pct:.1f}")
    path.write_text("\n".join([TREND_HEADER, *kept, new]) + "\n")
    return previous


# ---- output -----------------------------------------------------------------


def format_report(r: StatsReport, cfg: Config) -> list[str]:
    c, cl, cx = r.combined, r.per_tool["claude"], r.per_tool["codex"]
    zero = c.substantial - c.compliant
    zero_pct = 100.0 * zero / c.substantial if c.substantial else 0.0
    lines = [
        f"=== ctx stats (last {r.days} days) ===",
        f"PULL COMPLIANCE (combined): {c.compliant}/{c.substantial} substantial sessions read the corpus ({c.pct:.1f}%)",
        f"  claude: {cl.compliant}/{cl.substantial} ({cl.pct:.1f}%)",
        f"  codex:  {cx.compliant}/{cx.substantial} ({cx.pct:.1f}%)",
        f"Substantial sessions that read no corpus file: {zero}/{c.substantial} ({zero_pct:.1f}%)",
        f"Substantial sessions that wrote to the corpus: {c.wrote}/{c.substantial}",
        f"Total sessions: {c.sessions} (substantial: {c.substantial}; claude {cl.substantial}, codex {cx.substantial}; "
        f"substantial means {cfg.substantial_min_turns}+ assistant turns)",
    ]
    if c.sessions == 0:
        sources = [f"{name}: {glob}" for name, enabled, glob in
                   (("claude", cfg.claude_enabled, cfg.claude_glob), ("codex", cfg.codex_enabled, cfg.codex_glob)) if enabled]
        lines.append("NO TRANSCRIPTS FOUND in the window. A 0% here may mean a wrong glob, not unused notes. "
                     f"Enabled sources: {'; '.join(sources) or 'none'}. Check [sources] in the config.")
    lines += [
        "",
        "TOP READ (by distinct substantial sessions, all tools):",
    ]
    lines += [f"  {i:>2}. {path} ({count})" for i, (path, count) in enumerate(r.top_read, 1)] or ["  (none)"]
    lines += [
        "",
        f"DEAD WEIGHT: {len(r.dead)} file(s) read by no session in the window, all tools "
        f"(excludes README.md, {cfg.archive_dirname}/, excluded folders and files newer than {cfg.dead_weight_min_age_days}d)",
    ]
    lines += [f"  {path}" for path in r.dead[:20]]
    if len(r.dead) > 20:
        lines.append(f"  (+{len(r.dead) - 20} more not shown)")
    flag = f" -- FLAG: over {cfg.index_token_flag:,} estimated tokens" if r.index_flagged else ""
    ro = r.routing
    lines += [
        "",
        f"INDEX SIZE: README.md is {r.index_bytes:,} bytes (~{r.index_tokens:,} estimated tokens){flag}",
        f"INDEX ROUTING: {ro.routed}/{ro.read_index} substantial sessions that read the root README went on to read "
        f"another file ({ro.success_pct:.1f}% success, {ro.failed}/{ro.read_index} failed to route, {ro.failure_pct:.1f}% failure)",
        "Retrieval tooling is worth considering only when routing failure trends up or the index passes its token limit. "
        "Until then an index plus plain search is the cheaper mechanism.",
    ]
    return lines


def run(cfg: Config, days: int, now: float | None = None) -> int:
    """Entry point for ``ctx stats``. Records a trend row only for the canonical window."""
    corpus = Corpus(cfg)
    if not corpus.exists:
        print(f"ERROR: corpus root {cfg.root} does not exist", file=sys.stderr)
        return 1
    report = compute_stats(cfg, days, now)
    for line in format_report(report, cfg):
        print(line)
    print()
    today = date.fromtimestamp(now) if now is not None else date.today()
    if days == cfg.trend_window_days:
        previous = record_trend(cfg, report, today)
    else:
        previous = next((row for row in reversed(read_trend(cfg)) if row.date != today.isoformat()), None)
        print(f"(not recorded to the trend: only the canonical --days {cfg.trend_window_days} window is tracked)")
    if previous is None:
        print("Trend: no prior record")
    else:
        delta = report.combined.pct - previous.pct
        print(f"Trend: previous {previous.date} {previous.pct:.1f}% -> now {report.combined.pct:.1f}% ({delta:+.1f})")
    return 0

