"""``ctx schedule print``: render scheduler units for the recurring jobs. Print only.

Nothing here installs or enables anything, and scheduling is optional: every command
works when run by hand. The three jobs are:

* ``stats``     daily at 09:05, appends the day's compliance row
* ``snapshot``  hourly at minute 17, commits the corpus when it is dirty
* ``digest``    Monday at 09:15, writes the weekly report (and notifies if configured)

Every path in the output is absolute, because schedulers start with a nearly empty
environment and no working directory you can rely on.
"""

from __future__ import annotations

import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Config

LABEL_PREFIX = "dev.ctxkit"
UNIT_PREFIX = "ctxkit"


@dataclass(frozen=True)
class Job:
    name: str
    args: tuple[str, ...]
    when: str          # "daily", "hourly" or "weekly"
    minute: int
    hour: int | None = None
    weekday: int | None = None   # 1 = Monday


@dataclass(frozen=True)
class Context:
    """Everything a renderer needs, resolved to absolute values."""

    command: tuple[str, ...]
    config_path: str | None
    home: str
    state_dir: str
    path_env: str
    notify: bool = False
    snapshot_enabled: bool = True

    @property
    def log_dir(self) -> str:
        return f"{self.state_dir}/logs"

    def argv(self, job: Job) -> list[str]:
        base = list(self.command)
        if self.config_path:
            base += ["--config", self.config_path]
        extra = ["--notify"] if job.name == "digest" and self.notify else []
        return base + list(job.args) + extra

    def jobs(self) -> list[Job]:
        jobs = [Job("stats", ("stats",), "daily", minute=5, hour=9)]
        if self.snapshot_enabled:
            jobs.append(Job("snapshot", ("snapshot",), "hourly", minute=17))
        jobs.append(Job("digest", ("digest",), "weekly", minute=15, hour=9, weekday=1))
        return jobs


@dataclass(frozen=True)
class Rendered:
    filename: str
    content: str


def resolve_command(argv0: str | None = None) -> tuple[str, ...]:
    """The command that starts this tool: the ``ctx`` script it was launched through, else ``python -m``.

    The PATH is deliberately not searched. An unrelated program that happens to be called
    ``ctx`` must never be scheduled in place of this one. When the tool is run as a module
    the interpreter is named explicitly, which means ctxkit has to be installed for that
    interpreter, not merely importable through PYTHONPATH, for the scheduled job to work.
    """
    candidate = argv0 if argv0 is not None else (sys.argv[0] if sys.argv else "")
    if candidate and Path(candidate).name == "ctx":
        return (str(Path(candidate).resolve()),)
    return (sys.executable, "-m", "ctxkit")


def default_path_env(command: tuple[str, ...], which=shutil.which) -> str:
    """A PATH that still finds git and the tool itself when the scheduler provides almost none."""
    dirs: list[str] = []
    for exe in (which("git"), command[0]):
        if exe:
            dirs.append(str(Path(exe).parent))
    dirs += ["/usr/local/bin", "/usr/bin", "/bin"]
    if Path("/opt/homebrew/bin").is_dir():
        dirs.insert(0, "/opt/homebrew/bin")
    return ":".join(dict.fromkeys(dirs))


def context_from_config(cfg: Config, command: tuple[str, ...] | None = None,
                        path_env: str | None = None) -> Context:
    command = command or resolve_command()
    return Context(
        command=command,
        config_path=str(cfg.source_path) if cfg.source_path else None,
        home=str(Path.home()),
        state_dir=str(cfg.state_dir),
        path_env=path_env or default_path_env(command),
        notify=bool(cfg.notify_command),
        snapshot_enabled=cfg.snapshot_enabled,
    )


# ---- launchd ----------------------------------------------------------------


def _plist_schedule(job: Job) -> str:
    entries = []
    if job.weekday is not None:
        entries.append(("Weekday", job.weekday))
    if job.hour is not None:
        entries.append(("Hour", job.hour))
    entries.append(("Minute", job.minute))
    body = "\n".join(f"    <key>{k}</key>\n    <integer>{v}</integer>" for k, v in entries)
    return f"  <dict>\n{body}\n  </dict>"


def render_launchd(ctx: Context) -> list[Rendered]:
    out = []
    for job in ctx.jobs():
        args = "\n".join(f"    <string>{escape(a)}</string>" for a in ctx.argv(job))
        log = escape(f"{ctx.log_dir}/{job.name}.log")
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL_PREFIX}.{job.name}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>{escape(ctx.home)}</string>
    <key>PATH</key>
    <string>{escape(ctx.path_env)}</string>
  </dict>
  <key>StartCalendarInterval</key>
{_plist_schedule(job)}
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
</dict>
</plist>
"""
        out.append(Rendered(f"{LABEL_PREFIX}.{job.name}.plist", content))
    return out


# ---- systemd ----------------------------------------------------------------

_SYSTEMD_SAFE = re.compile(r"[A-Za-z0-9_@+=:,./-]+")


def systemd_quote(arg: str) -> str:
    """Quote one ExecStart word. systemd treats % and $ specially, so both are doubled."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if _SYSTEMD_SAFE.fullmatch(arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _on_calendar(job: Job) -> str:
    if job.when == "hourly":
        return f"*-*-* *:{job.minute:02d}:00"
    if job.when == "weekly":
        return f"Mon *-*-* {job.hour:02d}:{job.minute:02d}:00"
    return f"*-*-* {job.hour:02d}:{job.minute:02d}:00"


def render_systemd(ctx: Context) -> list[Rendered]:
    out = []
    for job in ctx.jobs():
        exec_start = " ".join(systemd_quote(a) for a in ctx.argv(job))
        service = f"""[Unit]
Description=ctxkit {job.name}

[Service]
Type=oneshot
Environment={systemd_quote("PATH=" + ctx.path_env)}
ExecStart={exec_start}
"""
        timer = f"""[Unit]
Description=Run ctxkit {job.name} {job.when}

[Timer]
OnCalendar={_on_calendar(job)}
Persistent=true

[Install]
WantedBy=timers.target
"""
        out.append(Rendered(f"{UNIT_PREFIX}-{job.name}.service", service))
        out.append(Rendered(f"{UNIT_PREFIX}-{job.name}.timer", timer))
    return out


# ---- cron -------------------------------------------------------------------


def _cron_fields(job: Job) -> str:
    if job.when == "hourly":
        return f"{job.minute} * * * *"
    if job.when == "weekly":
        return f"{job.minute} {job.hour} * * {job.weekday}"
    return f"{job.minute} {job.hour} * * *"


def render_cron(ctx: Context) -> list[Rendered]:
    lines = [
        "# ctxkit recurring jobs. Edit with: crontab -e",
        f"# Create the log folder once: mkdir -p {shlex.quote(ctx.log_dir)}",
        f"PATH={ctx.path_env}",
        f"HOME={ctx.home}",
    ]
    for job in ctx.jobs():
        command = " ".join(shlex.quote(a) for a in ctx.argv(job)).replace("%", "\\%")
        log = shlex.quote(f"{ctx.log_dir}/{job.name}.log").replace("%", "\\%")
        lines.append(f"{_cron_fields(job)} {command} >> {log} 2>&1")
    return [Rendered("crontab", "\n".join(lines) + "\n")]


# ---- printing ---------------------------------------------------------------

RENDERERS = {"launchd": render_launchd, "systemd": render_systemd, "cron": render_cron}


def install_notes(kind: str, ctx: Context) -> list[str]:
    """Comment lines describing where to save the files and how to enable them. Nothing is run."""
    if kind == "launchd":
        target = f"{ctx.home}/Library/LaunchAgents"
        labels = [f"{LABEL_PREFIX}.{j.name}" for j in ctx.jobs()]
        return [f"Save each file in {target}, create {ctx.log_dir}, then enable a job with:"] + [
            f"  launchctl bootstrap gui/$(id -u) {target}/{label}.plist" for label in labels
        ] + ["Disable with: launchctl bootout gui/$(id -u)/<label>"]
    if kind == "systemd":
        target = f"{ctx.home}/.config/systemd/user"
        timers = [f"{UNIT_PREFIX}-{j.name}.timer" for j in ctx.jobs()]
        return [f"Save each file in {target}, then enable the timers with:"] + [
            f"  systemctl --user enable --now {t}" for t in timers
        ] + ["Logs: journalctl --user -u 'ctxkit-*'"]
    return ["Paste the lines below into your crontab."]


def format_output(kind: str, ctx: Context) -> str:
    files = RENDERERS[kind](ctx)
    parts = [f"# {line}" for line in install_notes(kind, ctx)]
    for rendered in files:
        parts.append("")
        if kind != "cron":
            parts.append(f"# ---- file: {rendered.filename} ----")
        parts.append(rendered.content.rstrip("\n"))
    return "\n".join(parts) + "\n"


def run(cfg: Config, kind: str, command: tuple[str, ...] | None = None, path_env: str | None = None) -> int:
    ctx = context_from_config(cfg, command, path_env)
    sys.stdout.write(format_output(kind, ctx))
    return 0

