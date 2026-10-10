"""``ctx doctor``: integrity checks over the corpus.

Failures (exit 1): unreachable files, stale folder indexes, files with no date,
content loss since the last commit, a distilled file that regrew and has gone
quiet, and an over-budget root index.

Advisories (exit 0): gutted rule bodies, expired ``Verify by:`` markers, and the
first time a file crosses the size ceiling.

Content loss is keyed on identifiers, never prose. An identifier either exists
or it does not, so there is no inference and no false-positive class. The check
is corpus-wide, so moving a line between files never flags.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config, ensure_state_dir
from .corpus import Corpus, find_stale_folder_indexes, find_unreachable

DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
VERIFY_BY_RE = re.compile(r"Verify by:\s*(\d{4}-\d{2}-\d{2})")
BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")

# A rule whose line shrank below this fraction of its old length, from at least
# GUTTED_MIN_OLD_LEN bytes, is reported for a human to confirm.
GUTTED_SHRINK_RATIO = 0.5
GUTTED_MIN_OLD_LEN = 300

# Finding headings. The digest counts mentions of these in transcripts.
HEADINGS = (
    "ORPHANS",
    "STALE FOLDER INDEX",
    "MISSING DATE",
    "CONTENT LOST",
    "RULE BODIES SHRANK",
    "VERIFY BY",
    "REGROWN",
    "ROOT INDEX OVER BUDGET",
)

RECENT_SECONDS = 2 * 86400


# ---- result types -----------------------------------------------------------


@dataclass
class LossReport:
    ids: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    skipped: str = ""

    @property
    def lost(self) -> list[str]:
        return self.ids + [f'rule "{lead[:60]}"' for lead in self.rules]


@dataclass
class GuttedRule:
    path: str
    lead: str
    old_len: int
    new_len: int


@dataclass
class Expired:
    path: str
    due: str
    days_overdue: int


@dataclass
class FatFile:
    path: str
    size: int
    peaked: int = 0
    recent: bool = False


@dataclass
class RootIndexReport:
    size_bytes: int = 0
    over_entries: list[tuple[str, int]] = field(default_factory=list)
    token_limit: int = 0

    @property
    def tokens(self) -> int:
        return self.size_bytes // 4

    @property
    def over_tokens(self) -> bool:
        return self.tokens > self.token_limit

    @property
    def over_budget(self) -> bool:
        return self.over_tokens or bool(self.over_entries)


@dataclass
class Findings:
    orphans: list[str]
    stale: list[tuple[str, list[str]]]
    missing_date: list[str]
    loss: LossReport
    gutted: list[GuttedRule]
    expired: list[Expired]
    fat_first: list[FatFile]
    fat_regrown: list[FatFile]
    root_index: RootIndexReport

    @property
    def failed(self) -> bool:
        return bool(
            self.orphans or self.stale or self.missing_date or self.loss.lost
            or self.fat_regrown or self.root_index.over_budget
        )


# ---- git helpers ------------------------------------------------------------


def run_git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str] | None:
    """Run git inside ``root``. Returns None when git cannot be executed at all."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false", *args],
            capture_output=True, text=True, errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def has_head(root: Path) -> bool:
    proc = run_git(root, "rev-parse", "--verify", "-q", "HEAD", timeout=15)
    return proc is not None and proc.returncode == 0


# ---- individual checks ------------------------------------------------------


def check_missing_date(corpus: Corpus) -> list[str]:
    """Files with neither an ISO date nor an ``Updated:`` marker in their first 40 lines."""
    missing = []
    for path in corpus.files():
        try:
            with path.open(errors="ignore") as handle:
                head = "".join(itertools.islice(handle, 40))
        except OSError:
            continue
        if not DATE_RE.search(head) and "Updated:" not in head:
            missing.append(corpus.rel(path))
    return missing


def check_expired(corpus: Corpus, today: date | None = None) -> list[Expired]:
    """Files carrying a ``Verify by:`` date that has passed. Archived files are exempt.

    The marker is explicit and opt-in: the author committed to re-check a claim by
    a date. Nothing is inferred from file age or wording. Per file, the most
    overdue marker is reported.
    """
    today = today or date.today()
    out: list[Expired] = []
    for path in corpus.files():
        if corpus.is_archived(path):
            continue
        worst: tuple[int, str] | None = None
        for match in VERIFY_BY_RE.finditer(corpus.text(path)):
            try:
                due = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            overdue = (today - due).days
            if overdue > 0 and (worst is None or overdue > worst[0]):
                worst = (overdue, match.group(1))
        if worst:
            out.append(Expired(corpus.rel(path), worst[1], worst[0]))
    out.sort(key=lambda e: -e.days_overdue)
    return out


def check_loss(corpus: Corpus) -> LossReport:
    """Identifiers and rule leads present at git HEAD that are gone from the whole corpus now.

    Reads removed lines from ``git diff HEAD -U0``. Do not replace this with
    ``git grep -E``: that is POSIX ERE, rejects ``\\d`` and ``(?:``, and exits
    non-zero in a way a caller can mistake for "nothing lost".
    """
    cfg = corpus.cfg
    if not corpus.is_git_repo:
        return LossReport(skipped="the corpus root is not a git repository root, so loss cannot be detected. "
                                  "Run git init there to enable the check")
    if not has_head(corpus.root):
        return LossReport(skipped="no commits yet")
    proc = run_git(corpus.root, "diff", "HEAD", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--", "*.md")
    if proc is None or proc.returncode != 0:
        code = "unavailable" if proc is None else f"exited {proc.returncode}"
        return LossReport(skipped=f"git diff {code}")

    removed_ids: set[str] = set()
    removed_leads: set[str] = set()
    in_hunk = False
    for line in proc.stdout.splitlines():
        if line.startswith("diff --git"):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line.startswith("-"):
            body = line[1:]
            removed_ids.update(cfg.identifiers(body))
            lead = cfg.rule_lead(body)
            if lead:
                removed_leads.add(lead)
    if not removed_ids and not removed_leads:
        return LossReport()

    present_ids: set[str] = set()
    present_leads: set[str] = set()
    for path in corpus.files():
        text = corpus.text(path)
        present_ids.update(cfg.identifiers(text))
        for line in text.splitlines():
            lead = cfg.rule_lead(line)
            if lead:
                present_leads.add(lead)
    return LossReport(
        ids=sorted(removed_ids - present_ids),
        rules=sorted(removed_leads - present_leads),
    )


def check_gutted(corpus: Corpus) -> list[GuttedRule]:
    """Rules whose line shrank sharply while the bolded lead survived.

    Only files git reports as changed are inspected. Genuine tightening looks the
    same as gutting from here, so this is an advisory with numbers that make the
    human judgement quick.
    """
    cfg = corpus.cfg
    if not corpus.is_git_repo or not has_head(corpus.root):
        return []
    proc = run_git(corpus.root, "diff", "HEAD", "--name-only", "-z", "--no-renames", "--", "*.md", timeout=30)
    if proc is None or proc.returncode != 0:
        return []
    changed = [name for name in proc.stdout.split("\0") if name]

    def lead_lengths(text: str) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for line in text.splitlines():
            lead = cfg.rule_lead(line)
            if lead:
                sizes[lead] = len(line)
        return sizes

    out: list[GuttedRule] = []
    for rel in changed:
        path = corpus.root / rel
        if not path.is_file():
            continue
        old = run_git(corpus.root, "show", f"HEAD:{rel}", timeout=20)
        if old is None or old.returncode != 0:
            continue
        before, after = lead_lengths(old.stdout), lead_lengths(corpus.text(path))
        for lead, old_len in before.items():
            new_len = after.get(lead)
            if new_len is not None and old_len > GUTTED_MIN_OLD_LEN and new_len < old_len * GUTTED_SHRINK_RATIO:
                out.append(GuttedRule(rel, lead, old_len, new_len))
    out.sort(key=lambda g: g.old_len - g.new_len, reverse=True)
    return out


def classify_fat(corpus: Corpus, now: float | None = None) -> tuple[list[FatFile], list[FatFile]]:
    """Split oversized files into (first_time, regrown_and_dormant) and persist the history.

    Lifecycle, tracked per file in ``state_dir/fat-history.json``:

    * Over the ceiling for the first time: advisory.
    * Dropped back under: someone distilled it, so the file is armed.
    * Over the ceiling again while armed: advisory while the file is still being
      written (a busy file is large because the work is active), a failure once
      it has been quiet for ``regrown_dormant_days``.

    The armed bit stays set for as long as the file remains over the ceiling.
    Clearing it on the first report would turn the check into a one-shot warning.
    README files and archived files are exempt. A broken history file degrades to
    first-time behaviour instead of raising.
    """
    cfg = corpus.cfg
    now = time.time() if now is None else now
    path = cfg.state_dir / "fat-history.json"
    history: dict[str, dict[str, object]] = {}
    try:
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            history = {k: v for k, v in loaded.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        history = {}

    first_time: list[FatFile] = []
    regrown: list[FatFile] = []
    updated: dict[str, dict[str, object]] = {}
    for file in corpus.files():
        if file.name == "README.md" or corpus.is_archived(file):
            continue
        try:
            st = file.stat()
        except OSError:
            continue
        rel = corpus.rel(file)
        entry = history.get(rel, {})
        raw_peak = entry.get("peaked", 0)
        peaked = int(raw_peak) if isinstance(raw_peak, (int, float)) and not isinstance(raw_peak, bool) else 0
        armed = bool(entry.get("distilled"))
        if st.st_size > cfg.fat_file_bytes:
            idle_days = (now - st.st_mtime) / 86400
            recent = (now - st.st_mtime) < RECENT_SECONDS
            if armed and idle_days >= cfg.regrown_dormant_days:
                regrown.append(FatFile(rel, st.st_size, peaked, recent))
            else:
                first_time.append(FatFile(rel, st.st_size, peaked, recent))
            updated[rel] = {"peaked": max(st.st_size, peaked), "distilled": armed}
        elif peaked:
            updated[rel] = {"peaked": peaked, "distilled": True}

    try:
        ensure_state_dir(cfg)
        path.write_text(json.dumps(updated, indent=1, sort_keys=True))
    except OSError:
        pass
    first_time.sort(key=lambda f: -f.size)
    regrown.sort(key=lambda f: -f.size)
    return first_time, regrown


def fat_candidates(corpus: Corpus) -> list[FatFile]:
    """Every file over the size ceiling, largest first, with the same exemptions as ``classify_fat``."""
    out = []
    for file in corpus.files():
        if file.name == "README.md" or corpus.is_archived(file):
            continue
        try:
            size = file.stat().st_size
        except OSError:
            continue
        if size > corpus.cfg.fat_file_bytes:
            out.append(FatFile(corpus.rel(file), size))
    out.sort(key=lambda f: -f.size)
    return out


def check_root_index(corpus: Corpus) -> RootIndexReport:
    """Size of the root README and any single entry that is too long to be a pointer.

    An entry is a top-level bullet plus the lines that follow it, up to the next
    top-level bullet or heading. The root only routes. An entry past
    ``index_entry_max_bytes`` is a topic pasted into the index.
    """
    cfg = corpus.cfg
    report = RootIndexReport(token_limit=cfg.index_token_flag)
    readme = corpus.root / "README.md"
    try:
        text = readme.read_text(errors="ignore")
    except OSError:
        return report
    report.size_bytes = len(text.encode())

    current: list[str] | None = None

    def flush() -> None:
        if current:
            body = "\n".join(current).rstrip()
            size = len(body.encode())
            if size > cfg.index_entry_max_bytes:
                title = BULLET_RE.sub("", current[0], count=1).strip()
                report.over_entries.append((title, size))

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            current = None
        elif BULLET_RE.match(line):
            flush()
            current = [line]
        elif current is not None:
            current.append(line)
    flush()
    return report


# ---- aggregate --------------------------------------------------------------


def run_checks(corpus: Corpus, today: date | None = None, now: float | None = None) -> Findings:
    """Run every check. Also advances the persisted fat-file history."""
    first, regrown = classify_fat(corpus, now)
    return Findings(
        orphans=find_unreachable(corpus),
        stale=find_stale_folder_indexes(corpus),
        missing_date=check_missing_date(corpus),
        loss=check_loss(corpus),
        gutted=check_gutted(corpus),
        expired=check_expired(corpus, today),
        fat_first=first,
        fat_regrown=regrown,
        root_index=check_root_index(corpus),
    )


def format_findings(f: Findings, cfg: Config) -> list[str]:
    """Human readable output. Prints only problems."""
    out: list[str] = []
    if f.orphans:
        out.append(f"ORPHANS ({len(f.orphans)}): not reachable from README.md within three reads")
        out += [f"  {p}" for p in f.orphans]
    if f.stale:
        out.append("STALE FOLDER INDEX:")
        for folder, missing in f.stale:
            out.append(f"  {folder}/README.md does not name {len(missing)}: {', '.join(missing)}")
    if f.missing_date:
        out.append('MISSING DATE: no ISO date and no "Updated:" in the first 40 lines')
        out += [f"  {p}" for p in f.missing_date]
    if f.loss.skipped:
        out.append(f"(loss check skipped: {f.loss.skipped})")
    if f.loss.lost:
        shown = ", ".join(f.loss.lost[:12])
        extra = len(f.loss.lost) - 12
        if extra > 0:
            shown += f", and {extra} more"
        out.append(f"CONTENT LOST since the last commit. {len(f.loss.lost)} item(s) vanished from the whole corpus:")
        out.append(f"  {shown}")
        out.append("  These are gone everywhere, not moved. Find the removed lines with:")
        out.append(f"    git -C {cfg.root} diff HEAD -U0 | grep '^-'")
        out.append("  Reinstate them verbatim rather than re-summarising. If a removal was deliberate,")
        out.append("  commit it with a message saying so and this stops flagging.")
    if f.gutted:
        out.append("RULE BODIES SHRANK sharply while their bold leads survived. Confirm this was tightening, not loss:")
        for g in f.gutted[:6]:
            gone = 100 * (g.old_len - g.new_len) // g.old_len
            out.append(f'  {g.path}: "{g.lead[:52]}" {g.old_len} -> {g.new_len} B ({gone}% gone)')
        out.append("  Compare with: git diff HEAD -- <file>")
    if f.expired:
        out.append("VERIFY BY date has passed. Re-check the claim, then update or remove the marker:")
        out += [f"  {e.path} (due {e.due}, {e.days_overdue}d overdue)" for e in f.expired]
    if f.fat_regrown:
        out.append(
            "REGROWN and now DORMANT. Distilled once, grew back, and quiet for "
            f"{cfg.regrown_dormant_days}+ days, so it can be split calmly now. Fails the check:"
        )
        for x in f.fat_regrown:
            out.append(f"  {x.path} ({x.size / 1024:.1f} KB, previously peaked at {x.peaked / 1024:.1f} KB)")
        out.append("  Split into a companion file and index both. Do not trim content to fit.")
    if f.fat_first:
        hot = [x for x in f.fat_first if x.recent]
        older = len(f.fat_first) - len(hot)
        out.append(
            f"ADVISORY, does not fail the check. Files over {cfg.fat_file_bytes // 1000} KB: "
            "split into a companion when you next touch one, do not trim to fit."
        )
        for x in hot[:5]:
            out.append(f"  {x.path} ({x.size / 1024:.1f} KB, written recently)")
        hidden = older + max(0, len(hot) - 5)
        if hidden:
            out.append(f"  ...and {hidden} more. Full list: ctx distill")
    ri = f.root_index
    if ri.over_budget:
        out.append(
            f"ROOT INDEX OVER BUDGET: README.md is {ri.size_bytes // 1024} KB "
            f"(~{ri.tokens:,} tokens, limit {ri.token_limit:,}). Every session that opens it pays this."
        )
        for title, size in ri.over_entries[:8]:
            out.append(f"  entry over {cfg.index_entry_max_bytes} B: {title[:70]} ({size} B)")
        out.append("  Move detail into the folder or cluster index that owns it. The root only routes.")
    return out


def run(cfg: Config) -> int:
    """Entry point for ``ctx doctor``. Returns 1 when any failing check fires."""
    corpus = Corpus(cfg)
    if not corpus.exists:
        print(f"ERROR: corpus root {cfg.root} does not exist", file=sys.stderr)
        return 1
    findings = run_checks(corpus)
    for line in format_findings(findings, cfg):
        print(line)
    return 1 if findings.failed else 0
