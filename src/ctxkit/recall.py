"""``ctx recall``: can an agent find a fact in the corpus when given only clues?

Pull compliance says an agent opened files. It does not say the agent found what it
needed. This evaluation closes that gap with a small, repeatable quiz.

**build** samples facts from the corpus. A fact is a line that carries exactly one
identifier (a ticket key, say). The question is a keyword cue, not prose: the
identifier and markdown are removed, stopwords are dropped, and up to six content
words are kept, rarest first, where rarity is document frequency across the corpus.
A lexical oracle then runs an AND-grep of the cue words, every word on one line
across the whole corpus, to decide whether the question is fair:

* ``oracle_miss``: the grep does not land on the source file.
* ``ambiguous``: the grep also hits a line with a *different* identifier, so the
  cue does not pin down one answer.
* ``no_cue``: nothing distinctive was left after cleaning.

Unanswerable items are kept in the set for transparency and excluded from scoring.

**run** gives each answerable item to an external agent command and scores the last
line of its reply against the expected identifier. **report** shows the trend.

Sampling orders facts by a hash of the seed and the fact itself, then takes the
first N. The same seed and corpus always give the same set, and editing one fact
does not reshuffle the others.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Config, ensure_state_dir
from .corpus import Corpus

MAX_CUE_WORDS = 6
MIN_WORD_LENGTH = 3
SET_VERSION = 1

STOPWORDS = frozenset("""
about above after again all also and any are because been before being below between both but can could did
does doing down during each few for from further had has have having her here hers him his how into its just
may more most must nor not now off once only other our out over own per same she should some such than that
the their them then there these they this those through too under until use used uses using very was were what
when where which while who whom why will with would you your
""".split())

_FENCE = re.compile(r"^\s*(```|~~~)")
_TOKEN = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_TOKENS_USED = re.compile(r"tokens used\s*[:\n]?\s*([\d,]+)", re.IGNORECASE)


# ---- text helpers -----------------------------------------------------------


def strip_markdown(text: str) -> str:
    """Remove markup so that only the words a reader sees remain."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s*(?:[-*+>]|\d+[.)]|#{1,6})\s+", " ", text)
    return re.sub(r"[`*_~|]", " ", text)


def content_words(text: str) -> list[str]:
    """Distinct lowercase content words in first-seen order: no stopwords, no short words."""
    seen: dict[str, None] = {}
    for token in _TOKEN.findall(re.sub(r"'s\b", "", strip_markdown(text).lower())):
        if len(token) >= MIN_WORD_LENGTH and token not in STOPWORDS and token not in seen:
            seen[token] = None
    return list(seen)


@dataclass(frozen=True)
class Fact:
    path: str
    line: int
    identifier: str
    text: str


def eligible(corpus: Corpus, path: Path) -> bool:
    """Facts come from topic files, not indexes, archives or excluded folders."""
    return not (Corpus.is_index_file(path) or corpus.is_archived(path) or corpus.is_excluded(path))


def iter_facts(corpus: Corpus) -> Iterator[Fact]:
    """Lines outside code fences that carry exactly one distinct identifier."""
    for path in corpus.files():
        if not eligible(corpus, path):
            continue
        in_fence = False
        for number, line in enumerate(corpus.text(path).splitlines(), 1):
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            ids = set(corpus.cfg.identifiers(line))
            if len(ids) == 1:
                yield Fact(corpus.rel(path), number, next(iter(ids)), line.strip())


def document_frequency(corpus: Corpus) -> Counter[str]:
    """For each content word, how many corpus files contain it."""
    df: Counter[str] = Counter()
    for path in corpus.files():
        df.update(set(content_words(corpus.text(path))))
    return df


def make_cue(fact: Fact, df: Counter[str]) -> list[str]:
    """Up to six content words from the fact line, identifier removed, rarest first."""
    words = content_words(fact.text.replace(fact.identifier, " "))
    words.sort(key=lambda w: (df.get(w, 0), -len(w), w))
    return words[:MAX_CUE_WORDS]


class LineIndex:
    """Every corpus line, lowercased, with the identifiers on it: the haystack for the oracle."""

    def __init__(self, corpus: Corpus) -> None:
        self.entries: list[tuple[str, str, frozenset[str]]] = []
        for path in corpus.files():
            rel = corpus.rel(path)
            for line in corpus.text(path).splitlines():
                self.entries.append((rel, line.lower(), frozenset(corpus.cfg.identifiers(line))))

    def and_grep(self, cue: list[str]) -> tuple[set[str], set[str]]:
        """(files, identifiers) of lines where every cue word appears as a whole word."""
        patterns = [re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])") for w in cue]
        files: set[str] = set()
        ids: set[str] = set()
        for rel, lowered, line_ids in self.entries:
            if all(p.search(lowered) for p in patterns):
                files.add(rel)
                ids |= line_ids
        return files, ids


def judge(cue: list[str], fact: Fact, index: LineIndex) -> tuple[bool, str, list[str]]:
    """Decide whether the question is fair. Returns (answerable, reason, oracle files)."""
    if not cue:
        return False, "no_cue", []
    files, ids = index.and_grep(cue)
    if fact.path not in files:
        return False, "oracle_miss", sorted(files)
    if ids - {fact.identifier}:
        return False, "ambiguous", sorted(files)
    return True, "", sorted(files)


# ---- data model -------------------------------------------------------------


@dataclass
class Item:
    id: str
    answer: str
    path: str
    line: int
    cue: list[str]
    answerable: bool
    reason: str
    oracle_files: list[str]


@dataclass
class QuestionSet:
    version: int
    created: str
    root: str
    seed: int
    n_requested: int
    facts_available: int
    items: list[Item]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "QuestionSet":
        items = [Item(**item) for item in data["items"]]
        fields = {k: data[k] for k in ("version", "created", "root", "seed", "n_requested", "facts_available")}
        return cls(items=items, **fields)


@dataclass
class Record:
    item: str
    answer: str
    predicted: str = ""
    correct: bool = False
    abstained: bool = False
    timed_out: bool = False
    error: str | None = None
    seconds: float = 0.0
    tokens: int | None = None


# ---- build ------------------------------------------------------------------


def sample_key(seed: int, fact: Fact) -> str:
    return hashlib.sha256(f"{seed}\0{fact.path}\0{fact.identifier}\0{fact.text}".encode()).hexdigest()


def build_set(cfg: Config, n: int, seed: int, now: float | None = None) -> QuestionSet:
    """Sample ``n`` facts deterministically and judge each one."""
    corpus = Corpus(cfg)
    facts = sorted(iter_facts(corpus), key=lambda f: sample_key(seed, f))
    df = document_frequency(corpus)
    index = LineIndex(corpus)
    items = []
    for position, fact in enumerate(facts[:n], 1):
        cue = make_cue(fact, df)
        answerable, reason, files = judge(cue, fact, index)
        items.append(Item(f"q{position:02d}", fact.identifier, fact.path, fact.line, cue, answerable, reason, files))
    created = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return QuestionSet(
        version=SET_VERSION,
        created=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        root=str(cfg.root),
        seed=seed,
        n_requested=n,
        facts_available=len(facts),
        items=items,
    )


def recall_dir(cfg: Config) -> Path:
    return cfg.state_dir / "recall"


def save_set(cfg: Config, data: QuestionSet, today: date, out: Path | None = None) -> Path:
    ensure_state_dir(cfg)
    target = out or recall_dir(cfg) / f"set-{today.isoformat()}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(data), indent=1))
    return target


def cmd_build(cfg: Config, n: int, seed: int, out: Path | None = None, today: date | None = None) -> int:
    if not Corpus(cfg).exists:
        print(f"ERROR: corpus root {cfg.root} does not exist", file=sys.stderr)
        return 1
    data = build_set(cfg, n, seed)
    path = save_set(cfg, data, today or date.today(), out)
    reasons = Counter(i.reason for i in data.items if not i.answerable)
    answerable = sum(1 for i in data.items if i.answerable)
    print(f"recall set: {len(data.items)} item(s) from {data.facts_available} fact line(s), seed {seed}")
    print(f"  answerable: {answerable}")
    for reason, count in sorted(reasons.items()):
        print(f"  unanswerable ({reason}): {count}")
    if not data.items:
        print("  no facts found: a fact is a line with exactly one identifier outside indexes, archive and excluded folders")
    print(f"written: {path}")
    return 0


# ---- run --------------------------------------------------------------------


def build_prompt(root: Path, cue: list[str]) -> str:
    return (
        f"You have access to a read-only markdown knowledge base at {root}.\n"
        "Start from its README.md and follow the links to the relevant files. "
        "Never edit, create or delete any file.\n"
        "Find the identifier that goes with these clues. Exactly one identifier, such as an issue or "
        "ticket key, appears on the line that matches them.\n"
        f"Clues: {', '.join(cue)}\n"
        "Keep your reply short. Put only the identifier on the last line of your reply, "
        "or NONE if you cannot find it."
    )


@dataclass
class AgentOutput:
    text: str
    tokens: int | None
    is_error: bool = False


def parse_agent_output(stdout: str, stderr: str) -> AgentOutput:
    """Reply text and token usage when they can be recovered.

    Claude Code with ``--output-format json`` prints one JSON object holding ``result`` and
    ``usage``. Anything else is treated as plain text, and Codex-style ``tokens used`` lines
    are picked up from either stream.
    """
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        usage = data.get("usage")
        tokens = None
        if isinstance(usage, dict):
            counts = [v for k in USAGE_KEYS if isinstance(v := usage.get(k), int) and not isinstance(v, bool)]
            tokens = sum(counts) if counts else None
        return AgentOutput(data["result"], tokens, bool(data.get("is_error")))
    match = _TOKENS_USED.search(stderr) or _TOKENS_USED.search(stdout)
    tokens = int(match.group(1).replace(",", "")) if match else None
    return AgentOutput(stdout, tokens)


def extract_prediction(text: str, cfg: Config) -> str:
    """The identifier named on the last non-empty line, or NONE."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1].strip().strip("`*_\"'. \t")
    if last.upper() == "NONE":
        return "NONE"
    ids = set(cfg.identifiers(last))
    return next(iter(ids)) if len(ids) == 1 else last


def latest_set(cfg: Config) -> Path | None:
    sets = sorted(recall_dir(cfg).glob("set-*.json"))
    return sets[-1] if sets else None


def run_one(cfg: Config, item: Item) -> Record:
    """Ask the agent one question. Raises OSError when the command cannot be started."""
    prompt = build_prompt(cfg.root, item.cue)
    argv = [part.replace("{prompt}", prompt) for part in cfg.recall_agent_command]
    record = Record(item=item.id, answer=item.answer)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
            timeout=cfg.recall_timeout_seconds, cwd=str(cfg.root),
        )
    except subprocess.TimeoutExpired:
        record.timed_out = True
        record.seconds = round(time.monotonic() - started, 3)
        return record
    record.seconds = round(time.monotonic() - started, 3)
    parsed = parse_agent_output(proc.stdout, proc.stderr)
    record.tokens = parsed.tokens
    if proc.returncode != 0 or parsed.is_error:
        detail = f": {proc.stderr.strip()[:200]}" if proc.stderr.strip() else ""
        record.error = f"exit {proc.returncode}{detail}"
        return record
    record.predicted = extract_prediction(parsed.text, cfg)
    record.abstained = record.predicted == "NONE"
    record.correct = record.predicted.casefold() == item.answer.casefold()
    return record


@dataclass
class Summary:
    run_id: str
    started: str
    set: str
    seed: int
    agent: str
    n_items: int
    n_answerable: int
    answerable_rate: float
    n_run: int
    n_correct: int
    accuracy: float
    n_abstained: int
    n_timeouts: int
    n_errors: int
    median_seconds: float | None
    median_tokens: float | None
    corpus_modified_during_run: bool


def summarise(run_id: str, started: datetime, set_name: str, data: QuestionSet, cfg: Config,
              records: list[Record], changed: bool) -> Summary:
    answerable = sum(1 for i in data.items if i.answerable)
    correct = sum(1 for r in records if r.correct)
    tokens = [float(r.tokens) for r in records if r.tokens is not None]
    return Summary(
        run_id=run_id,
        started=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        set=set_name,
        seed=data.seed,
        agent=Path(cfg.recall_agent_command[0]).name,
        n_items=len(data.items),
        n_answerable=answerable,
        answerable_rate=answerable / len(data.items) if data.items else 0.0,
        n_run=len(records),
        n_correct=correct,
        accuracy=correct / len(records) if records else 0.0,
        n_abstained=sum(1 for r in records if r.abstained),
        n_timeouts=sum(1 for r in records if r.timed_out),
        n_errors=sum(1 for r in records if r.error),
        median_seconds=statistics.median(r.seconds for r in records) if records else None,
        median_tokens=statistics.median(tokens) if tokens else None,
        corpus_modified_during_run=changed,
    )


def cmd_run(cfg: Config, set_path: Path | None = None, limit: int | None = None) -> int:
    corpus = Corpus(cfg)
    if not corpus.exists:
        print(f"ERROR: corpus root {cfg.root} does not exist", file=sys.stderr)
        return 1
    if not any("{prompt}" in part for part in cfg.recall_agent_command):
        print("ERROR: [recall] agent_command must be an argv list containing {prompt}. See examples/config.toml.",
              file=sys.stderr)
        return 2
    path = set_path or latest_set(cfg)
    if path is None or not path.is_file():
        print("ERROR: no recall set found. Run `ctx recall build` first.", file=sys.stderr)
        return 2
    try:
        data = QuestionSet.from_json(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: cannot read recall set {path}: {exc}", file=sys.stderr)
        return 2

    answerable = [i for i in data.items if i.answerable]
    to_run = answerable[:limit] if limit else answerable
    before = corpus.fingerprint()
    started = datetime.now(timezone.utc)
    records: list[Record] = []
    for item in to_run:
        try:
            record = run_one(cfg, item)
        except OSError as exc:
            print(f"ERROR: cannot start the agent command {cfg.recall_agent_command[0]!r}: {exc}", file=sys.stderr)
            return 2
        records.append(record)
        status = "ok" if record.correct else "timeout" if record.timed_out else "error" if record.error else "miss"
        print(f"  {item.id} {item.answer}: {status} ({record.seconds}s)")

    changed = corpus.fingerprint() != before
    if changed:
        print("WARNING: the corpus changed while the agent ran. It was meant to be read-only, so check the "
              "agent_command permissions before trusting this run.", file=sys.stderr)

    run_id = started.strftime("%Y%m%dT%H%M%S")
    summary = summarise(run_id, started, path.name, data, cfg, records, changed)
    ensure_state_dir(cfg)
    out_dir = recall_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"run-{run_id}.jsonl").write_text("".join(json.dumps(asdict(r)) + "\n" for r in records))
    (out_dir / f"run-{run_id}.summary.json").write_text(json.dumps(asdict(summary), indent=1))

    print(f"recall run {run_id} on {path.name}")
    print(f"  accuracy: {summary.n_correct}/{summary.n_run} ({100 * summary.accuracy:.1f}%)")
    print(f"  answerable: {summary.n_answerable}/{summary.n_items} ({100 * summary.answerable_rate:.1f}%)")
    print(f"  median time: {_fmt(summary.median_seconds, 1, 's')}, median tokens: {_fmt(summary.median_tokens, 0)}")
    if summary.n_timeouts or summary.n_errors or summary.n_abstained:
        print(f"  timeouts {summary.n_timeouts}, errors {summary.n_errors}, abstained {summary.n_abstained}")
    return 0


def _fmt(value: float | None, digits: int, unit: str = "") -> str:
    return "n/a" if value is None else f"{value:.{digits}f}{unit}"


# ---- report -----------------------------------------------------------------


def load_summaries(cfg: Config) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(recall_dir(cfg).glob("run-*.summary.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("accuracy"), (int, float)):
            out.append(data)
    out.sort(key=lambda d: str(d.get("started", "")))
    return out


def cmd_report(cfg: Config) -> int:
    runs = load_summaries(cfg)
    if not runs:
        print("no recall runs yet. Run `ctx recall build`, then `ctx recall run`.")
        return 0
    print(f"RECALL TREND ({len(runs)} run(s)). Accuracy is comparable only between runs on the same set.")
    print(f"{'started':<21}{'set':<24}{'correct':>9}{'accuracy':>10}{'change':>9}{'answerable':>12}"
          f"{'median s':>10}{'median tok':>12}")
    previous_by_set: dict[str, float] = {}
    for r in runs:
        accuracy = float(r["accuracy"])
        set_name = str(r.get("set", ""))
        before = previous_by_set.get(set_name)
        change = f"{100 * (accuracy - before):+.1f}" if before is not None else "new set"
        previous_by_set[set_name] = accuracy
        print(f"{str(r.get('started', '')):<21}{set_name:<24}"
              f"{str(r.get('n_correct')) + '/' + str(r.get('n_run')):>9}{100 * accuracy:>9.1f}%{change:>9}"
              f"{100 * float(r.get('answerable_rate', 0)):>11.1f}%"
              f"{_fmt(r.get('median_seconds'), 1):>10}{_fmt(r.get('median_tokens'), 0):>12}")
    return 0
