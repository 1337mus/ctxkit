"""Command line entry point: ``ctx`` or ``python -m ctxkit``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config

DESCRIPTION = """\
Audit, measure and gently nudge a markdown knowledge base that coding agents pull from.

Nothing here injects text into an agent's context. ctx inspects the corpus, reads agent
transcripts, and reports. Every command works when run by hand; scheduling is optional.
"""

EPILOG = """\
configuration lookup, first match wins:
  --config PATH, then $CTXKIT_CONFIG, then ~/.config/ctxkit/config.toml, then built-in defaults.
  See examples/config.toml for every key and its default.

exit status: 0 success, 1 a check failed or an operation could not complete, 2 usage or config error.
`ctx hook stop` always exits 0 and prints {} on any problem.
"""


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _add_config(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS keeps a value given before the subcommand from being overwritten by the default.
    parser.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS,
                        help="configuration file (see the lookup order below)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ctxkit {__version__}")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="configuration file (see the lookup order below)")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        _add_config(p)
        return p

    command("doctor", "check corpus integrity: reachability, indexes, dates, content loss, size limits (exit 1 on failure)")

    p = command("stats", "pull compliance, top-read files, dead weight and index routing from agent transcripts")
    p.add_argument("--days", type=_positive, default=None, metavar="N",
                   help="window in days (default: trend_window_days; only that window is written to the trend)")

    p = command("hook", "agent hook handlers")
    hook_sub = p.add_subparsers(dest="hook_command", metavar="<hook>", required=True)
    hp = hook_sub.add_parser("stop", help="Stop hook: ask for a capture when work has gone unrecorded",
                             description="Reads a hook payload as JSON on stdin. Always exits 0.")
    _add_config(hp)
    hp.add_argument("--tool", default="claude", metavar="claude|codex",
                    help="which hook dialect to answer in (default: claude)")

    command("snapshot", "commit the corpus if it is dirty, unless identifier loss is detected")

    p = command("digest", "write a one-page markdown health report, tripped conditions first")
    p.add_argument("--notify", action="store_true", help="run [digest] notify_command afterwards")

    p = command("distill", "print a promotion brief for an agent to act on")
    p.add_argument("--days", type=_positive, default=7, metavar="N", help="look back this many days (default 7)")

    p = command("schedule", "render scheduler units (print only, never installs)")
    sched_sub = p.add_subparsers(dest="schedule_command", metavar="<action>", required=True)
    sp = sched_sub.add_parser("print", help="print unit files or crontab lines for stats, snapshot and digest")
    _add_config(sp)
    group = sp.add_mutually_exclusive_group(required=True)
    group.add_argument("--launchd", action="store_const", dest="kind", const="launchd", help="macOS launch agents")
    group.add_argument("--systemd", action="store_const", dest="kind", const="systemd", help="systemd user units")
    group.add_argument("--cron", action="store_const", dest="kind", const="cron", help="crontab lines")

    p = command("recall", "evaluate whether an agent can find facts in the corpus")
    recall_sub = p.add_subparsers(dest="recall_command", metavar="<step>", required=True)
    rb = recall_sub.add_parser("build", help="sample facts and write a question set")
    _add_config(rb)
    rb.add_argument("--n", type=_positive, default=None, help="number of facts (default: [recall] n)")
    rb.add_argument("--seed", type=int, default=None, help="sampling seed (default: [recall] seed)")
    rb.add_argument("--out", type=Path, default=None, help="write the set here instead of state_dir/recall")
    rr = recall_sub.add_parser("run", help="ask the configured agent each answerable question and score it")
    _add_config(rr)
    rr.add_argument("--set", type=Path, default=None, dest="set_path", help="question set (default: newest)")
    rr.add_argument("--limit", type=_positive, default=None, help="only run the first K answerable items")
    _add_config(recall_sub.add_parser("report", help="show the accuracy trend across past runs"))

    command("config", "print the effective configuration and where it came from")
    return parser


def _load(args: argparse.Namespace) -> Config | None:
    try:
        cfg = load_config(getattr(args, "config", None))
    except ConfigError as exc:
        print(f"ctx: config error: {exc}", file=sys.stderr)
        return None
    for warning in cfg.warnings:
        print(f"ctx: warning: {warning}", file=sys.stderr)
    return cfg


def _hook_stop(args: argparse.Namespace) -> int:
    """Always exit 0 and print JSON, whatever goes wrong."""
    import json

    from . import hook

    try:
        cfg = load_config(getattr(args, "config", None))
        raw = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
        answer = hook.handle_stop(cfg, args.tool, raw)
    except Exception:
        answer = {}
    print(json.dumps(answer))
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "hook":
        return _hook_stop(args)
    cfg = _load(args)
    if cfg is None:
        return 2

    if args.command == "doctor":
        from . import doctor
        return doctor.run(cfg)
    if args.command == "stats":
        from . import stats
        return stats.run(cfg, args.days or cfg.trend_window_days)
    if args.command == "snapshot":
        from . import snapshot
        return snapshot.run(cfg)
    if args.command == "digest":
        from . import digest
        return digest.run(cfg, notify=args.notify)
    if args.command == "distill":
        from . import distill
        return distill.run(cfg, args.days)
    if args.command == "schedule":
        from . import schedule
        return schedule.run(cfg, args.kind)
    if args.command == "recall":
        from . import recall
        if args.recall_command == "build":
            return recall.cmd_build(cfg, args.n or cfg.recall_n,
                                    cfg.recall_seed if args.seed is None else args.seed, args.out)
        if args.recall_command == "run":
            return recall.cmd_run(cfg, args.set_path, args.limit)
        return recall.cmd_report(cfg)
    if args.command == "config":
        sys.stdout.write(cfg.to_toml())
        return 0
    return 2  # pragma: no cover  (argparse requires a known command)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return an exit status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code != 0 and "hook" in argv and "stop" in argv:
            # A hook that exits non-zero can wedge the harness that called it.
            print("{}")
            return 0
        return code
    try:
        return _dispatch(args)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
