# ctxkit

**A memory for your coding agents, kept in plain markdown files.**

Your agents read a folder of notes before they work and write new notes when they learn something. ctxkit keeps that folder healthy, makes sure nothing in it gets lost, and shows you whether the agents actually use it.

It works with **Claude Code** and **Codex**. It is one small command, `ctx`, with no dependencies beyond Python.

---

## 🚀 Quickstart

```bash
pipx install git+https://github.com/1337mus/ctxkit && ctx init
```

That creates your knowledge base at `~/.agent-context` and prints a short snippet. Paste the snippet into the `AGENTS.md` or `CLAUDE.md` your agent reads. You are done.

---

## 🧭 How it works

```mermaid
flowchart LR
    A["🤖 Your agents<br/>Claude Code, Codex"]
    KB[("📁 Knowledge base<br/>markdown files in git")]
    H["🔔 Stop hook<br/>asks for notes after real work"]
    D["🩺 Doctor<br/>finds broken links, lost notes, bloat"]
    S["💾 Snapshot<br/>commits every hour"]
    M["📊 Stats, recall, digest<br/>is it working?"]

    A -->|"① read"| KB
    A -->|"② turn ends"| H
    H -->|"③ write it down"| A
    A -->|"④ save notes"| KB
    D --> KB
    S --> KB
    M -.->|"reads session logs"| A
```

There are three parts:

- **The knowledge base** is a folder of markdown files in git. You own it. You can read it, edit it and grep it.
- **The agents** read the index first, then open only the notes they need.
- **ctxkit** sits beside both. It reminds agents to write things down, checks the folder for damage, commits it, and measures how well it is working.

ctxkit never puts text into an agent's prompt. The agent decides what to read.

### One session, step by step

```mermaid
sequenceDiagram
    participant A as Agent
    participant K as Knowledge base
    participant C as ctxkit

    A->>K: Read README.md, open the right note
    A->>A: Do the work
    A->>C: Turn ends (stop hook)
    C-->>A: "You did 8 edits and wrote nothing down"
    A->>K: Record the decision, gotcha or deploy state
    A->>C: ctx doctor
    C-->>A: All good
    C->>K: Hourly snapshot commit
```

---

## 🤔 Why use it

Coding agents forget everything between sessions. You end up explaining the same decisions again, and agents repeat mistakes that were already solved.

The usual fix is a memory system that searches past notes and injects the best matches into every prompt. In the deployment where this tool was built, that approach was measured before it was replaced:

| Approach | Result |
| --- | --- |
| Inject the top matches into every prompt | Relevant **7%** of the time |
| Let the agent read an index and open what it needs | Agents used the notes in **68%** of sessions, unprompted |

So ctxkit keeps the simple approach and fixes the parts that break in practice:

- Agents forget to write things down.
- Notes get silently deleted during routine edits.
- The index slowly grows until it is too expensive to read.
- Nobody can tell whether any of it is working.

---

## ✅ Advantages

- **Nothing to run.** No database, no server, no embeddings, no API keys.
- **You can read everything.** It is just markdown in git. If ctxkit disappeared tomorrow, your notes would still be there.
- **Nothing gets lost quietly.** If a ticket key or a rule disappears from the whole folder, `ctx doctor` fails and tells you how to get it back.
- **Agents write as they go.** The stop hook asks for notes after real work, not only at the end of a session.
- **You get real numbers.** How often agents read the notes, which notes matter, and whether a fresh agent can find a stored fact.
- **It works across tools.** Claude Code and Codex share one knowledge base.
- **Nothing touches your prompts.** Unrelated text can't distract the agent, because ctxkit adds none.

---

## 🎯 When to use it

**Use it if:**

- you use coding agents every day on the same projects,
- you find yourself re-explaining decisions, setups or past incidents,
- you run more than one agent tool and want them to share what they know,
- you want memory you can inspect and fix by hand.

**Skip it if:**

- you only use agents for one-off tasks,
- your notes are already in a wiki your agents can't reach and you don't plan to move them,
- you need memory shared by a large team. ctxkit is tested with one person and one machine.

---

## 🛠️ Commands

You mostly need the first two.

| Command | What it does |
| --- | --- |
| `ctx init` | Create a knowledge base and print the agent snippet |
| `ctx doctor` | Check the folder for broken links, lost notes and bloat |
| `ctx stats` | Show how often agents read the notes and which ones |
| `ctx recall build` then `ctx recall run` | Quiz a fresh agent on facts from your notes |
| `ctx digest` | Write a one-page weekly health report |
| `ctx snapshot` | Commit the folder, unless something was lost |
| `ctx hook stop` | The stop hook your agent tool calls |
| `ctx schedule print --launchd` | Print scheduler files for the daily, hourly and weekly jobs (optional) |

---

## 📊 What the numbers mean

| Number | It tells you | It does not tell you |
| --- | --- | --- |
| **Pull rate** | How many sessions opened at least one note | Whether it was the right note |
| **Recall accuracy** | Whether a fresh agent can find a fact from a few keywords | Whether an agent remembers to look at the moment it matters |
| **Dead weight** | Notes nobody opened lately | That they are useless. Some notes are rarely needed. |
| **Index size** | What it costs a session to read the index | Anything about quality |

---

## 📚 Learn more

- [docs/reference.md](docs/reference.md): every command and check in detail
- [docs/integrations.md](docs/integrations.md): wiring the hook into Claude Code and Codex
- [docs/design.md](docs/design.md): the lessons behind the design
- [examples/config.toml](examples/config.toml): every setting, with its default and the reason for it

---

## Status

**Alpha.** Tested by one person on macOS with Claude Code and Codex. It should work on Linux. Expect the session log parsing to need updates when the agent tools change their formats.

**License:** to be decided before public release.
