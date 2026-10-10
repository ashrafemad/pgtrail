import argparse
import json
import os
import sys

import psycopg

from . import __version__
from .actual import render_actual
from .arcade import PACES, Arcade, ArcadeUnavailable, play
from .catch import CatchGame
from .maze import MazeGame
from .render import render
from .tracer import TraceError, run_trace

THEMES = {"classic": Arcade, "catch": CatchGame, "maze": MazeGame}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="pgtrail",
        description="Follow one SQL statement through PostgreSQL, layer by layer.",
    )
    ap.add_argument("sql", help="the statement to run (it really runs; see --rollback)")
    ap.add_argument("-d", "--dsn", default=os.environ.get("PGTRAIL_DSN", ""),
                    help="libpq connection string; default: $PGTRAIL_DSN, then the usual PG* variables")
    ap.add_argument("--rollback", action="store_true", help="roll the transaction back instead of committing it: everything is "
                         "still traced, the data ends up unchanged")
    ap.add_argument("--cold", action="store_true",
                    help="trace on a brand new backend, including its one-off catalog cache loading")
    ap.add_argument("--checkpoint", action="store_true",
                    help="run a CHECKPOINT once the transaction ends, so the dirty pages are "
                         "written to their data files and you see it happen")
    ap.add_argument("--os", dest="os_trace", action="store_true",
                    help="also trace syscalls with strace (must run on the database host)")
    ap.add_argument("--protocol", choices=["extended", "simple"], default="extended",
                    help="how the statement is sent: extended (Parse, Bind, Describe, Execute, "
                         "Sync, as applications do; the default) or simple (one Query message, "
                         "as psql does)")
    ap.add_argument("--lock-wait", type=int, default=0, metavar="MS",
                    help="have a second session lock the statement's first table for MS "
                         "milliseconds, so you see the statement wait for it")
    ap.add_argument("--with", dest="with_sql", metavar="SQL",
                    help="another session runs SQL first and keeps its transaction open for "
                         "--lock-wait ms (default 500): see whether the statement waits for it")
    ap.add_argument("--deadlock", metavar="SQL",
                    help="with --with: this session runs SQL first, the other session runs --with "
                         "and then SQL; the statement closes the cycle and PostgreSQL's deadlock "
                         "detector breaks it")
    ap.add_argument("--max-pages", type=int, default=8,
                    help="how many changed pages the PAGES section reads back (default 8)")
    ap.add_argument("--theme", choices=sorted(THEMES),
                    help="replay the trace as an animation with this look: classic (80x24), "
                         "catch (a monster-catching adventure, 100x30) or maze (a maze chase, "
                         "100x30)")
    ap.add_argument("--pace", choices=[*PACES, "actual"],
                    help="how long each step of the animation stays on screen to be read: "
                         "normal, slow or slower (default normal). While it plays: space next "
                         "step, p pause, q quit. actual skips the animation and prints at once "
                         "how long the statement really took")
    ap.add_argument("--detail", choices=["summary", "detailed", "full"], default="full",
                    help="how much to show: summary (one screen, the core steps), detailed "
                         "(every layer, without the server's trees, page bytes and syscalls) or "
                         "full (everything; the default)")
    ap.add_argument("--json", action="store_true", help="print the raw trace as JSON")
    ap.add_argument("--no-color", action="store_true",
                    help="plain text without colours (automatic when the output is not a terminal)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    if args.deadlock and not args.with_sql:
        ap.error("--deadlock needs --with: the other session's first statement")

    try:
        trace = run_trace(args.dsn, args.sql, commit=not args.rollback, cold=args.cold,
                          checkpoint=args.checkpoint, os_trace=args.os_trace, max_pages=args.max_pages,
                          protocol=args.protocol,
                          # the trees travel as extra notices: leave them out when timing is the point
                          show_trees=args.pace != "actual" and args.detail == "full",
                          lock_wait_ms=max(0, args.lock_wait), with_sql=args.with_sql,
                          deadlock_sql=args.deadlock)
    except TraceError as e:
        sys.exit(f"pgtrail: {e}")
    except psycopg.OperationalError as e:
        sys.exit(f"pgtrail: cannot connect: {str(e).strip()}")

    # what to show, first match wins: --json, --pace actual, an animation, the report
    if args.json:
        _print(json.dumps(trace, indent=2, default=str))
        return

    if args.pace == "actual":
        sys.stdout.write(render_actual(trace, color=False if args.no_color else None))
        return

    # any theme or animation pace you type, even the defaults, asks for the animation
    if args.theme or args.pace in PACES:
        try:
            play(trace, story=THEMES[args.theme or "classic"], pace=args.pace or "normal",
                 detail=args.detail)
        except ArcadeUnavailable as e:
            sys.exit(f"pgtrail: {e}")
        return

    _print(render(trace, color=False if args.no_color else None, detail=args.detail))


def _print(text):
    try:
        print(text)
    except BrokenPipeError:  # e.g. piped into head
        sys.stderr.close()


if __name__ == "__main__":
    main()
