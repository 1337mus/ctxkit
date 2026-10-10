# Integrations

How ctxkit connects to coding agents, schedulers and notification tools. ctxkit reads transcripts and answers hook calls. It never writes to an agent's own configuration.

## What ctxkit reads

Two transcript sources, each switchable in `[sources.claude]` and `[sources.codex]`:

| Tool | Default glob | Used for |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/*/*.jsonl` | stats, digest friction, hook |
| Codex | `~/.codex/sessions/*/*/*/rollout-*.jsonl` | stats, digest friction, hook |

Other tools are not instrumented. If a tool can run a shell command at the end of a turn and you can read its session files, support could be added in `transcripts.py`, which turns each format into one stream of events: assistant turns, reads, writes under the corpus, edits elsewhere, and shell commands.

### Claude Code line shape

Lines of `"type": "assistant"` carry `message.content[]`. Items of type `tool_use` are the tool calls.

* A read is a `Read` call with `input.file_path`.
* An edit is `Write`, `Edit`, `MultiEdit` or `NotebookEdit`. The notebook tool names its path `notebook_path`.
* A shell command is a `Bash` call with `input.command`.

Edits under `~/.claude` and `~/.codex` are ignored as project work, since those folders hold the agent's own plans and memory.

### Codex line shape

Lines are `{"timestamp", "type", "payload"}`.

* `response_item` with a `message` payload and role `assistant` is a turn.
* `response_item` with a `custom_tool_call` payload carries its text in `input`. A `function_call` payload carries it in `arguments`, usually a JSON string such as `{"cmd": "..."}`. Versions differ on which field holds the text, so both are read for every call, and JSON strings are decoded.
* `event_msg` with a `patch_apply_end` payload has a `changes` object keyed by file path. That is the authoritative record of files a patch wrote, one key per file, so a patch that touched nine files counts as nine edits. A patch with `success: false` is ignored.

Codex has no read tool, so reads are inferred from call text. A path under the corpus root that appears in a call counts as a read unless the call is writing it. A path is a write when it follows `>`, `>>` or `tee`, when the call is an apply_patch body (only the `*** Add/Update/Delete File:` headers count, since the rest of a patch only quotes text), or when the same command uses `sed -i`, `perl -i`, `rm`, `mv`, `touch` or `truncate`. Paths may be absolute, or start with `~/`, `$HOME/` or `${HOME}/` when the root is under your home folder. Only paths with a file extension count, so listing a folder or expanding a glob is not a read.

Parsing follows the shape of each line and not the tool a file came from. The `--tool` flag only chooses which answer dialect the hook prints.

## The stop hook

`ctx hook stop` reads one JSON object on stdin. It looks for a session id and a transcript path, accepting several spellings of each (`session_id`, `sessionId`, `conversation_id`, `thread_id`, `id`; `transcript_path`, `transcriptPath`, `rollout_path`, `session_file`). If a Codex payload names no transcript, the newest rollout file under the Codex glob is used, which can pick the wrong one when two sessions run at once. If no session id is given, the transcript path becomes the key.

It answers `{}` to do nothing. To ask for a capture it answers in the dialect of the tool:

| Tool | Answer |
| --- | --- |
| Claude Code | `{"decision": "block", "reason": "<message>"}` |
| Codex | `{"continue": false, "stopReason": "<message>"}` |

It always exits 0 and prints `{}` on any problem: unreadable payload, unknown tool, missing transcript, broken config, an exception. It stays silent when the payload carries a true `stop_hook_active` (so a stop that is already a continuation is never blocked again), and when the file `state_dir/disabled` exists.

### When it fires

The two counters are edits outside the corpus and milestones. A milestone is one of:

* `gh pr create`, `merge`, `edit`, `ready`, `review` or `close`
* `git commit`, `push`, `merge`, `rebase`, `cherry-pick` or `revert`, with or without `-C path` or `-c key=value` before the verb
* `gh api ... -X POST`, `PATCH`, `PUT` or `DELETE` (or `--method`), where the match cannot cross `&&`, `;` or `|` into a later command

Read-only commands such as `gh pr view`, `gh api` with GET, `git status` or `git log` never count.

It blocks when either unrecorded count reaches its threshold (`nudge_min_edits`, default 8, or `nudge_min_milestones`, default 5). It then records the totals so far in `state_dir/nudged-<session>` as `{"e": N, "m": N}` and stays quiet until another full threshold of unrecorded work builds up, giving a cadence of 8, 16, 24 edits in a long session. Any write under the corpus root counts as a capture and starts a fresh window for both counters, even if it happens after a nudge and the next nudge would otherwise have been a threshold away.

`state_dir/last-hook-payload-<tool>.json` keeps the names and types of the fields the last payload carried, never their values, so you can see what a harness sends when its contract changes.

### Claude Code

Add the hook to `~/.claude/settings.json` for every project, or to a project's `.claude/settings.json` for that project only:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "ctx hook stop" } ] }
    ]
  }
}
```

If `ctx` is not on the path that Claude Code runs with, use the absolute path, or `python -m ctxkit hook stop` with the interpreter's absolute path. To use a config file other than the default, put `--config /absolute/path` before `hook`.

### Codex

Register `ctx hook stop --tool codex` as a stop hook, in the hooks file format that your Codex version documents. Two things are worth knowing:

* A new hook entry has no trust record. The first interactive Codex session asks you to approve it. A non-interactive `codex exec` run does not prompt and will wait, which is the usual sign that approval is missing.
* In the deployment ctxkit was built in, the approval was tied to the hook entry and not to the contents of the `ctx` program, so upgrading ctxkit kept working. Editing the entry required approving again. Check this against your Codex version.

Whether an interactive Codex session acts on the block and performs the capture should be confirmed in your own sessions. The hook is known to fire, count and answer in the right dialect. A one-shot `codex exec` ends after the block because it has no further turn to continue into.

## Instruction files

The pull convention belongs in whichever file your agent reads at the start of a session: `AGENTS.md`, `CLAUDE.md`, or a global equivalent. A snippet is in the [README](../README.md#the-pull-convention). Keep it short and point at the corpus root. Anything longer starts to be a second copy of the corpus.

## Running recall against an agent

`[recall] agent_command` is an argument list containing `{prompt}`. It runs in the corpus folder with no shell. Use only read-only tools.

```toml
# Claude Code, with a read-only tool list. --output-format json exposes usage, so tokens are recorded.
agent_command = ["claude", "-p", "{prompt}", "--output-format", "json", "--model", "sonnet", "--allowedTools", "Read,Grep,Glob"]

# Codex, in its read-only sandbox. Token usage is read from a "tokens used" line when present.
# agent_command = ["codex", "exec", "--sandbox", "read-only", "{prompt}"]
```

Any other agent works if it takes the prompt as an argument and prints its reply, with the identifier or `NONE` on the last line. Token counts are recorded only for the two shapes above. ctxkit fingerprints the corpus before and after a run and warns if a file changed.

## Notifications

`ctx digest --notify` runs `[digest] notify_command`, an argument list in which `{path}` is the digest file and `{summary}` is a one-line summary:

```toml
# macOS
notify_command = ["osascript", "-e", "display notification \"{summary}\" with title \"ctx digest\""]
# Linux desktop
# notify_command = ["notify-send", "ctx digest", "{summary}"]
# Anything else: a script that posts to a chat tool, mails a file, and so on
# notify_command = ["/path/to/my-notifier", "{path}", "{summary}"]
```

A failing notify command is reported on stderr and does not fail the digest.

## Scheduling

`ctx schedule print` renders units for three jobs and prints them. See [`scheduling/`](../scheduling/README.md) for example output and install notes. Schedulers start with a minimal environment, so the rendered units set `HOME` and a `PATH` that includes git, and use absolute paths for everything.

## Corpus repository checks in CI

If you keep the corpus in its own repository, `ctx doctor` works as a CI check, since its exit status is 1 on failure. The loss check needs history, so fetch enough of it for `git diff HEAD` to have a baseline. In a fresh checkout nothing is uncommitted and the loss check has nothing to compare, which is correct: it protects work in progress, and `ctx snapshot` plus a clean history protects the rest.

## State directory

Everything ctxkit writes lives under `state_dir`, apart from commits made by `ctx snapshot` and the digest folder if you moved it.

| Path | Contents |
| --- | --- |
| `compliance.tsv` | One row per day for the canonical window. Columns: date, substantial sessions, percent, then per-tool counts and percents. |
| `fat-history.json` | Per-file size history that arms the regrowth check. |
| `nudged-<session>` | Hook totals at the last nudge. |
| `last-hook-payload-<tool>.json` | Field names the last hook payload carried. |
| `disabled` | Create it to silence the stop hook. |
| `recall/set-<date>.json` | A question set. |
| `recall/run-<time>.jsonl` and `.summary.json` | Results and summary of a recall run. |
| `digest/<date>.md` and `latest.md` | Digests. |
