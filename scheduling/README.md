# Scheduling examples

Scheduling is optional. Every `ctx` command works when you run it by hand.

These files are example output of `ctx schedule print`, rendered for a made-up setup: the program at `/usr/local/bin/ctx`, a config at `/home/you/.config/ctxkit/config.toml`, and a home folder of `/home/you`. Run `ctx schedule print --launchd`, `--systemd` or `--cron` to get the same thing with your own absolute paths. The command only prints. It never installs or enables anything.

The program path comes from how you started the tool. Run through the installed `ctx` script, the units name that script. Run as `python -m ctxkit`, they name the Python interpreter and `-m ctxkit`, so ctxkit must be installed for that interpreter and not merely importable through `PYTHONPATH`. The `PATH` is never searched, so an unrelated program called `ctx` cannot be scheduled by mistake.

Three jobs are rendered:

| Job | When | What it does |
| --- | --- | --- |
| `stats` | Daily at 09:05 | Appends the day's compliance row to the trend. |
| `snapshot` | Hourly at minute 17 | Commits the corpus if it is dirty and nothing was lost. Left out when `[snapshot] enabled = false`. |
| `digest` | Mondays at 09:15 | Writes the weekly digest. Adds `--notify` when a `notify_command` is configured. |

## Where files go

* **launchd (macOS):** `launchd/*.plist` into `~/Library/LaunchAgents/`. Create the log folder first, then `launchctl bootstrap gui/$(id -u) <plist>`. Undo with `launchctl bootout gui/$(id -u)/<label>`. The example uses a Linux style home path because that is what the sample context says. Yours will show your own.
* **systemd (Linux):** `systemd/*` into `~/.config/systemd/user/`, then `systemctl --user enable --now ctxkit-stats.timer` and likewise for the other timers.
* **cron:** the lines in `cron/crontab` into `crontab -e`. Create the log folder first.

## Why units set HOME and PATH

Schedulers start jobs with a nearly empty environment. The rendered units set `HOME`, and a `PATH` that includes the folder holding git and the folder holding `ctx`, so that snapshots can run git and the tool can find its config. Every path is absolute because there is no working directory to rely on.

A test renders the sample context and compares it with these files, so they cannot drift from the renderer.
