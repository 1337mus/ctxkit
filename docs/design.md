# Design notes

These notes explain why ctxkit works the way it does. Each section is a lesson that came from running a markdown knowledge base for coding agents in daily use. Where a number appears, it was measured in a single-user deployment, so treat it as an illustration of scale and not as a benchmark.

## Pull beats push, measured

The first design question is how knowledge reaches an agent. The tempting answer is retrieval: embed the notes, rank them against the task, and inject the best few snippets into every session. It feels like the sophisticated choice.

The measurements pointed the other way. In the deployment, agents opened the notes folder on their own in about 68 percent of substantial sessions, with nothing but a short line in their instruction file. An injection scheme run earlier was judged relevant in about 7 percent of the sessions it fired in. Injected text also has a cost beyond the tokens: text that is coherent but unrelated to the task pulls attention away from the work, and the agent cannot tell which part to ignore.

At the size of a personal or small team corpus, an index file plus plain search is cheap, transparent and easy to debug. When something goes wrong you can read the index and see why. So ctxkit never injects anything. It checks the corpus, measures use, and nudges the agent to write things down. It keeps two tripwires for the day this stops being enough: an index that costs too many tokens to open, and a rising share of sessions that open the index and then open nothing else. The first response to either is to split the index by domain, not to add a vector store.

## A fixed-size store needs an eviction policy, a destination and an audit trail before it needs a ceiling

An early version capped the standing rules file at a fixed size and told agents to cut rules that no longer earned their place. Agents did exactly that. Several rules written a few days earlier were gone with no record of what had been removed or why.

A ceiling with no eviction policy, no destination for what is evicted and no audit trail does not compress anything. It deletes, and it deletes whatever the current reader judges least established, which is usually the newest entry. If a store must have a limit, decide first what leaves, where it goes, and how someone can see that it left. In ctxkit the answer to growth is always to move content to a companion file and index both. Nothing is trimmed to fit a number.

## Most loss is ordinary editing, so key a detector on identifiers

After the ceiling was fixed, the next night's routine condensing pass dropped five identifiers from a file that was nowhere near any limit. One of them was a deployment hash and another carried a rollback procedure. Nothing noticed.

Size pressure is one cause of loss. Ordinary editing is a bigger one. Anyone who tidies a file can drop a sentence, and the file still reads well afterwards. So a store worth keeping needs a loss detector that does not depend on what you think caused the loss.

The detector keys on identifiers such as ticket keys, because an identifier either exists or it does not. There is no judgement to make and no way to be wrong about a match. It reads the removed lines from the version control diff and checks whether each identifier is still present anywhere in the corpus. The search is corpus wide on purpose, so moving a line to another file never raises an alarm. Only an identifier that has vanished everywhere is a loss.

Prose rules carry no identifiers, so the same idea is applied to the one structural handle they do have: a bolded lead at the start of the rule. A rule whose lead disappears is reported. A rule whose lead survives while its body shrinks to under half is reported as an advisory with the before and after sizes. In one case a rule was cut from 1,180 bytes to 451 while keeping its heading, and neither of the other checks could see it. Whether such a change is tightening or gutting is a human judgement, so this one never fails the gate. It only makes the judgement quick.

## Reachability over centralization

It is natural to require that the root index list every file, so that nothing is lost. That requirement makes the root grow with the collection, and every session that opens it pays for the whole catalogue to find one entry. In the deployment the root index reached 63 KB, around 15,700 tokens, while about 90 percent of its entries duplicated a folder index that listed the same file. Rewriting the root as a router, with a line per folder, brought it to 7.4 KB and made nothing unreachable.

What matters is that every file can be reached, not that one document names it. ctxkit enforces reachability within three reads: the root names the file, or the root names a folder and the folder README names the file, or the folder README names a cluster index that names the file. The chain stops at three on purpose, because a longer chain stops being navigation. A cluster index that nobody names cannot vouch for anything, so splitting an index cannot launder an orphan. A folder that outgrew one README split into clusters, and one folder index fell from 46 KB to 11 KB with every entry moved unchanged.

The checks also learned a lesson about names. Matching a file name as a substring means that a note called `notes.md` looks reachable whenever `api-notes.md` is mentioned. ctxkit matches whole names only.

## Removing a bad limit needs a replacement

The size cap on the standing file was removed once it was clear it produced byte shaving instead of compression. An agent that was 93 bytes over the limit spent several turns trimming rules to get under it. That was the right call. But a week later the file had grown to 131 KB, because nothing replaced the cap.

The choice is not between a hard cap and nothing. What worked was a wide advisory threshold, 40 KB by default, whose instruction is to split rather than trim. The pressure still exists, but its output is a second file instead of a smaller one. The same trap caught the agents that maintained the file: several sessions in a row shaved bytes off it to satisfy the check, while the file's own header named that exact anti-pattern. When you catch yourself freeing bytes to fit a lesson in, the split is overdue.

## A gate that punishes activity creates a treadmill

A file that had been split once and then crossed the limit again originally failed the check immediately. In a single week that produced 484 failures across about twenty sessions, all on files that were large because the project was active. Seventeen companion files were created under pressure, and sessions spent their time shrinking files to clear a gate in the middle of a task.

Before failing on a condition, ask whether it is a defect or just work in progress seen mid-flight. A busy file is large because the work is happening, and splitting it then is churn against the tide. It also lands on whichever agent happens to be running. Splitting is judgement work that is best done when a file goes quiet. So the check now fails only after the file has gone unwritten for a week, and until then the same condition is an advisory. The guarantee is intact, since nothing stays large forever. Every file eventually cools.

The state has to survive. The first version of this lifecycle reported the failure once, cleared its own flag, and every later run saw an ordinary advisory. It had rebuilt the one-shot warning it was meant to replace. The armed flag now stays set for as long as the file remains over the limit, and a test runs the check three times to prove it.

## Advisories that print everything get skimmed

The list of large files grew to eighteen lines and was printed at every checkpoint. It buried the real failures above it, and agents learned to skip the whole block.

An advisory should print what the reader can act on and count the rest. Now only recently written files are listed, because the session that wrote them is the right one to split them, and everything older collapses to one line with a count. The full list is one command away. The same principle shapes the doctor's output: it prints only problems, and silence means success.

## An archive tier needs judgment, not a sweep

Finished work needs somewhere to go that is out of the way but still findable. An archive folder does that. Files in it stay reachable and searchable, and they are exempt from size, expiry and dead-weight checks, because an archived project's claims are a historical record and not a prompt to review.

The tempting automation is a sweep: move everything nobody has opened lately. A mechanical pass over files with no reads and no writes in a month produced 44 candidates. A reader going through them one at a time archived 10 and kept 34, most often because a file that was still busy named the candidate as its living companion or authority. Cold by access count is not the same as closed. Only reading tells the two apart, so ctxkit produces the candidate list and leaves the decision to an agent that reads.

Moves are done with version control so that history follows the file, and every inbound reference is updated to the new path. Do not leave a redirect stub at the old location. A stub is a place where agents keep writing, because that is the path they already know.

## The system should report itself

A scorecard that only answers when someone asks arrives too late. At one point the author asked whether the whole project was failing, while its own metrics showed it at its healthiest. Meanwhile the root index had grown from 14.5 KB to 76 KB over six weeks, passed its tripwire, and been reported by three weekly digests without anyone acting.

Reporting a number is not the same as raising a condition. The digest leads with a section of everything that has crossed a limit, in plain words, before any trend. The index budget also moved from a statistic into the gate, so crossing it fails the check instead of waiting to be noticed. A weekly digest is cheap to schedule and cheap to read, which is why it exists.

Its friction section counts keyword mentions of doctor findings in transcripts. That count includes sessions that were discussing a finding without hitting it, so it is a rough signal. The digest says so in its own text.

## Commits must not depend on agent habit

For about six weeks, agents in the deployment stopped committing, and close to a megabyte of notes sat with no history. The loss detector compares against the last commit, so its baseline was six weeks stale and it protected almost nothing.

Anything that depends on an agent remembering will fail quietly. So `ctx snapshot` commits the corpus whenever it is dirty, on a schedule. It refuses to commit while an identifier loss exists, because committing would turn the loss into the new baseline and silence the detector. Commits are unsigned by default, since an unattended job cannot answer a passphrase prompt and would hang rather than fail.

## Explicit expiry, never inferred

A claim about current state, such as which version is deployed, goes stale. Detecting that automatically is tempting and does not work. A version that guessed from file age and in-flight wording flagged a pile of files and missed the single claim known to be false, which sat in a file barely a day old. Age does not correlate with wrongness.

ctxkit reads an explicit marker instead: `Verify by:` followed by a date. Only a marker somebody chose to write is ever reported, so there are no false positives by construction. Re-checking the claim and moving the date forward is the suppression, so no rejection ledger is needed. It is an advisory, because "still true, new date" is a legitimate result.

## What the system cannot see

Contradictions between files are out of reach. Two notes that assert opposite things are a semantic problem, and the lexical tools in this kit will not find them. An agent reading both files found the one known case. If you attempt contradiction detection, expect a lexical approach to fail.

## Two triggers for the nudge

The stop hook originally counted file edits. Measured on a long review and merge session, there were 66 git and gh write actions and only 6 file edits. That kind of session produces the most durable facts, such as merge states, decisions and ordering, and an edit counter would never have fired for it. So the hook has two independent triggers, edits and milestones, and reads read-only polling as noise: a watch loop of status queries produced dozens of false milestones before the count was restricted to write verbs.

The hook keeps cumulative totals and the totals at the last nudge and at the last capture. Work that is still unrecorded is the total minus whichever of those two marks is later. An earlier version stored a count since the last capture and discounted its marker only when it saw the count drop. When a nudge fired at the threshold, the agent captured, and then reached the threshold again before the hook next ran, the stale marker doubled the wait. Cumulative totals have no state that can go stale.

## A recall evaluation is needed because compliance is not recall

Pull compliance counts sessions that opened a file. It cannot say whether the agent found what it came for. A session that opens the index, wanders, and gives up counts the same as one that finds the answer in two reads.

The recall evaluation is a small quiz built from the corpus itself. It takes facts that carry exactly one identifier, turns each into a keyword cue, and asks an agent to name the identifier. A lexical oracle removes unfair questions, such as cues with two correct answers, so a miss means the agent failed and not the quiz. Because the set is chosen by hash, it stays stable as the corpus grows, and accuracy can be trended over time. It will not tell you whether an answer was used well, and it measures one agent with one prompt. It does close the gap that compliance leaves open, and it is the number to watch when you change how the corpus is organized.
