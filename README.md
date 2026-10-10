# ctxkit

ctxkit audits, measures and gently nudges a folder of markdown notes that coding agents read. It is a small command line tool, `ctx`, written in Python with no dependencies beyond the standard library.

The notes are a knowledge base for your agents: decisions, deploy state, gotchas, rules you have learned. ctxkit does not put any of it into an agent's context window. It checks that the notes stay healthy, reports whether agents actually use them, and asks an agent to write things down at the right moments.

**Status: alpha.** It has been tested in a single-user deployment, against one person's corpus and two agent tools. The commands are stable enough to use. Expect rough edges, and expect the transcript parsing in particular to need updates when the agent tools change their formats.

**License: to be decided before public release.**

## The core idea: agents pull, nothing is injected

There are two ways to give an agent knowledge. You can push it, by ranking and injecting snippets into every session, or you can let the agent pull it, by pointing it at a folder and letting it read what it needs.

Measured in a single-user deployment, agents pulled from the corpus unprompted in about 68 percent of substantial sessions, while an injection scheme that pushed context was judged relevant in about 7 percent of the sessions it fired in. Injected text that does not fit the task also costs attention. So ctxkit takes the pull side and does three things around it:

1. It keeps the corpus navigable and lossless, with a gate called `ctx doctor`.
2. It measures whether agents pull, with `ctx stats`, and whether pulling works, with `ctx recall`.
3. It nudges agents to write durable context back, with a hook that fires at the end of a turn.

## Quickstart

```bash
# 1. Install (Python 3.11 or newer).
pip install .            # or run without installing: PYTHONPATH=src python -m ctxkit

# 2. Create a corpus and put it under git, so loss can be detected.
mkdir -p ~/.agent-context && cd ~/.agent-context && git init
cat > README.md <<'EOF'
# Knowledge base

Updated: 2026-01-01

Read this first. Open the folder or file you need.

- [standing-rules.md](standing-rules.md): short rules for every project.
EOF
printf '# Standing rules\n\nUpdated: 2026-01-01\n' > standing-rules.md

# 3. Check it.
ctx doctor            # prints only problems, exits 1 if any check fails

# 4. Tell your agents to use it (see "The pull convention" below), then watch usage.
ctx stats
```

Copy `examples/config.toml` to `~/.config/ctxkit/config.toml` to change any default. Every key is documented there.

## How the corpus should look

ctxkit assumes a few conventions. None are hard to adopt, and each one exists so that a mechanical check can protect something.

* **A root `README.md` that routes.** It names folders and key files. It does not need to list everything.
* **A `README.md` in each folder** that names the files in that folder.
* **Cluster indexes.** When a folder README gets long, move entries into sibling files called `something-index.md` and name those from the folder README.
* **An `archive/` folder** for finished work.
* **A dated header.** An ISO date or an `Updated:` line in the first 40 lines of every file.
* **Identifiers.** Ticket keys such as `SHOP-123` next to the decisions they belong to.
* **Rules with bold leads.** A bullet or numbered item that opens with a bolded imperative of at least twelve characters.
* **Optional expiry.** A claim that will not stay true can carry `Verify by: 2026-11-01` using any future date.

A file is reachable if an agent can find it in at most three reads: the root names it, or the root names its folder and the folder README names it, or the folder README names a cluster index that names it. A cluster index that nobody names cannot vouch for anything.

## Commands

| Command | What it does |
| --- | --- |
| `ctx doctor` | Integrity gate. Exit 1 on failure. Prints only problems. |
| `ctx stats [--days N]` | Pull compliance, top-read files, dead weight, index size and routing. Appends one trend row a day. |
| `ctx hook stop [--tool claude\|codex]` | Stop hook. Reads a payload on stdin, may block once to ask for a capture. Always exits 0. |
| `ctx snapshot` | Commit the corpus if it is dirty, unless identifier loss exists. |
| `ctx digest [--notify]` | One-page markdown health report, tripped conditions first. |
| `ctx distill [--days N]` | A brief for a promotion pass: what changed, what is large, what is unread. |
| `ctx schedule print --launchd\|--systemd\|--cron` | Print scheduler units for stats, snapshot and digest. Never installs. |
| `ctx recall build\|run\|report` | Evaluate whether an agent can find facts in the corpus. |
| `ctx config` | Print the effective configuration and where it was loaded from. |

### `ctx doctor`

Fails, with exit status 1, on any of these:

* **Unreachable files**, using the three-read rule above.
* **Stale folder indexes**: a folder README that does not name some file in its folder, directly or through a cluster index it names.
* **Missing dates**.
* **Content lost since the last commit.** (If the corpus root is not a git repository, doctor prints a line saying the check was skipped. Run `git init` there to enable it.) Identifiers that appeared on removed lines in `git diff HEAD -U0` and are now absent from the whole corpus, and bold rule leads that have vanished. The check is corpus wide, so moving a line from one file to another never flags. Identifiers are matched by the patterns in `identifier_patterns` minus `identifier_ignore`.
* **A regrown file that has gone quiet.** A file that was split below the size limit, grew back over it, and has not been written for `regrown_dormant_days`. While the file is still being written the same condition is only an advisory. The armed state persists in `state_dir/fat-history.json` for as long as the file stays over the limit.
* **An over-budget root index.** The root README over `index_token_flag` estimated tokens, or any single entry over `index_entry_max_bytes`.

Advisories, which do not change the exit status:

* **Rule bodies that shrank sharply** (under half their old length, from over 300 bytes) while the bold lead survived. Only files git reports as changed are inspected.
* **Expired `Verify by:` markers.** Archived files are exempt.
* **Files over `fat_file_bytes`.** README files and archived files are exempt. Only recently written files are listed, with a count of the rest.

### `ctx stats`

Reads the transcripts of Claude Code and Codex sessions, and prints:

* **Pull compliance**: of substantial sessions (at least `substantial_min_turns` assistant turns), the share that read at least one corpus file, per tool and combined.
* **Top read files**, by distinct substantial sessions.
* **Dead weight**: files no session read in the window. Excludes READMEs, the archive, excluded folders and files newer than `dead_weight_min_age_days`.
* **Index size** in bytes and estimated tokens, flagged over `index_token_flag`.
* **Index routing**: of substantial sessions that read the root README, how many went on to read another file.

Dead weight and routing count reads from both tools. Only the canonical window, `trend_window_days`, is appended to `state_dir/compliance.tsv`, one row per day, so ad hoc runs cannot distort the series.

### `ctx hook stop`

A rolling checkpoint with two independent triggers: unrecorded file edits outside the corpus, and unrecorded git or gh write actions. Any write under the corpus root counts as a capture and resets both. Details and wiring are in [docs/integrations.md](docs/integrations.md).

### `ctx recall`

A small quiz over your own corpus. `build` samples facts, which are lines carrying exactly one identifier, and turns each into a keyword cue. `run` hands each cue to an agent you configure and checks the identifier it answers with. `report` shows the trend. See [Recall](#recall) below.

## The pull convention

Add a short block to the `AGENTS.md` or `CLAUDE.md` that your agent reads, adjusted to your corpus path:

```markdown
## Knowledge base

Before non-trivial work, read ~/.agent-context/README.md and open the files it routes you to.
Search the folder with grep before guessing. When you learn something durable, such as a
decision, a gotcha or a deploy state, write it into the matching file there. Keep ticket keys
and dates. Never delete a fact to make a file shorter: move it to a companion file. Run
`ctx doctor` before you finish and fix what it reports.
```

This is a pull instruction. It tells the agent where to look. It does not carry any of the knowledge itself.

## Hook wiring

The stop hook runs when an agent finishes a turn. When enough work has gone unrecorded it answers with a block and a message asking the agent to record durable context, promote general lessons to the first file in `standing_files`, and run `ctx doctor`.

Claude Code, in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "ctx hook stop" } ] }
    ]
  }
}
```

Codex: register the command `ctx hook stop --tool codex` as a stop hook, using the hooks file format of your Codex version. Codex asks you to approve a new hook once, and the approval has to happen in an interactive session. A non-interactive `codex exec` waits instead of asking. In the deployment this was built in, the approval covered the hook entry rather than the program it launches, so updating ctxkit did not require approving again, but editing the entry did.

To silence the hook without removing it, create the file `state_dir/disabled`. Delete it to turn the hook back on.

## Configuration

Lookup order, first match wins: `--config PATH`, `$CTXKIT_CONFIG`, `~/.config/ctxkit/config.toml`, then built-in defaults. A path given by flag or environment variable that does not exist is an error, not a silent fallback. [`examples/config.toml`](examples/config.toml) documents every key with its default and the reason for it. Unknown keys produce a warning on stderr.

## What each metric measures, and what it does not

| Metric | It tells you | It does not tell you |
| --- | --- | --- |
| Pull compliance | The share of substantial sessions that opened at least one corpus file. | Whether the right file was opened, whether the answer was found, or whether it was used. |
| Top read | Which files carry the traffic. | Whether they are good. Popular can mean central or can mean confusing. |
| Dead weight | Files nobody opened in the window. | That a file is useless. A rarely needed runbook is supposed to look like this. It is a list to read, not a list to delete. |
| Index size | The per-session cost of opening the root. | Anything about quality. |
| Index routing | Whether sessions that open the root go on to open something else. | Whether they opened the right thing. A session that opens the root and then searches with grep counts as failed routing. |
| Recall accuracy | Whether an agent can find a fact from keyword clues, on a fair question set. | Anything about questions that need reasoning, or facts without an identifier. It is one agent, one model and one prompt. |
| Digest friction | Roughly how often agents ran into doctor findings. | An exact count. It counts keyword mentions, including sessions that merely discussed a finding. |

Known blind spots in transcript reading:

* Claude Code reads are `Read` tool calls. A file opened through a shell `cat` in Claude Code is not counted.
* Codex has no read tool, so reads are inferred from command text: a corpus path that appears in a call and is not being written. Relative paths with a separate working directory are invisible, and directories and globs are ignored on purpose.
* Edits made by a shell redirect do not count as edits for the hook. File edits are counted from Claude Code edit tools and from Codex patch results.
* Milestones are matched in command text, so a quoted string containing `git push` counts.
* Sessions are counted by assistant turns. A Claude Code transcript can split one message across several lines, so its turn count runs a little high.

## Recall

Compliance says an agent opened files. It does not say the agent found what it needed. The recall evaluation measures that.

**Build.** `ctx recall build --n 20 --seed 1` samples facts, meaning lines with exactly one identifier, from topic files. It skips index files (`README.md` and `*-index.md`), excluded folders and the archive. The question for each fact is a keyword cue: the identifier and markdown are removed, stopwords dropped, and up to six content words kept, rarest first by document frequency across the corpus. Sampling orders facts by a hash of the seed and the fact, so the same seed gives the same set and editing one fact does not reshuffle the rest.

A lexical oracle then checks whether the question is fair. It runs an AND-grep of the cue words, all on one line, over the whole corpus. An item is unanswerable if the grep misses its source file, if the grep also hits a line with a different identifier so the cue has two valid answers, or if no distinctive words are left. Unanswerable items stay in the saved set for transparency and are excluded from scoring.

**Run.** `ctx recall run` gives each answerable item to the `[recall] agent_command` you configure, with a prompt saying there is a read-only knowledge base at your root, to start from its `README.md`, to find the identifier matching the clues, never to edit files, and to put only the identifier or `NONE` on the last line. It enforces `timeout_seconds`, runs the agent with the corpus as its working directory, and records correctness, wall time and token usage when it can parse it. It writes a JSON lines file of results and a summary: accuracy, answerable rate, median time and median tokens.

The agent command must be read-only. ctxkit compares the corpus before and after a run and warns if anything changed, but it cannot stop a write-capable agent. `examples/config.toml` shows a read-only Claude Code command and a Codex command.

**Report.** `ctx recall report` lists past runs. Accuracy is comparable only between runs on the same question set, and the report says so by marking the first run on each set.

## Scheduling

Everything works by hand. If you want it on a schedule, `ctx schedule print --launchd`, `--systemd` or `--cron` prints units for three jobs: stats daily, snapshot hourly, digest weekly. It prints only. Nothing is installed or enabled for you. Example output lives in [`scheduling/`](scheduling/).

## Development

```bash
python -m unittest discover -s tests          # standard library only, no install needed
PYTHONPATH=src python -m ctxkit --help
```

Tests build their git repositories in a temporary directory and never touch your home folder. Fixtures are an invented online bookstore.

The design reasoning is in [docs/design.md](docs/design.md). Integration details are in [docs/integrations.md](docs/integrations.md).

## Platform notes

Developed on macOS. It should work on Linux. Windows is untested. `git` must be on the path for the loss checks and snapshots.
