"""Runs one statement on a dedicated backend while a second "observer"
connection watches from outside, so the measuring never runs on the backend
being measured."""
import tempfile
import time
from datetime import datetime, timezone

import psycopg

from .collectors import (btree, buffers, contention, locks, pages, plans, stages, statistics,
                         syscalls, trees, wal)
from .collectors.protocol import parse_trace
from .rels import Rels

EXTENSIONS = ("pageinspect", "pg_buffercache", "pg_walinspect")
SESSION = (
    "auto_explain.log_min_duration = 0",
    "auto_explain.log_analyze = on",
    "auto_explain.log_buffers = on",
    "auto_explain.log_wal = on",
    "auto_explain.log_timing = on",
    # verbose would add schema names, and looking those up touches
    # pg_namespace pages that the statement itself never needs
    "auto_explain.log_verbose = off",
    "auto_explain.log_format = json",
    "log_parser_stats = on",
    "log_planner_stats = on",
    "log_executor_stats = on",
    "track_io_timing = on",
    # deliver the instrumentation to us, and keep it out of the server log
    "log_min_messages = fatal",
    "client_min_messages = log",
)
SETTINGS_OF_INTEREST = (
    "shared_buffers", "block_size", "wal_level", "synchronous_commit", "fsync",
    "full_page_writes", "wal_sync_method", "checkpoint_timeout", "work_mem",
)
BACKGROUND = ("checkpointer", "background writer", "walwriter")
# --trees: the backend sends its query, rewritten and plan trees as LOG notices
TREES = ("debug_print_parse = on", "debug_print_rewritten = on", "debug_print_plan = on",
         "debug_pretty_print = on")


class TraceError(Exception):
    pass


def single_statement(sql):
    """True unless a second statement follows a ';' outside quotes."""
    i, n, seen_end = 0, len(sql), False
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            i = sql.find("\n", i)
            if i < 0:
                return True
        elif sql.startswith("/*", i):
            i = sql.find("*/", i)
            if i < 0:
                return True
            i += 1
        elif seen_end and not c.isspace():
            return False
        elif c in "'\"":
            i = sql.find(c, i + 1)
            if i < 0:
                return True
        elif c == "$":
            j = sql.find("$", i + 1)
            tag = sql[i : j + 1] if j > 0 else ""
            if tag and (tag == "$$" or tag[1:-1].isidentifier()):
                i = sql.find(tag, j + 1)
                if i < 0:
                    return True
                i += len(tag) - 1
        elif c == ";":
            seen_end = True
        i += 1
    return True


def _prepare_server(obs):
    num = int(obs.execute("show server_version_num").fetchone()[0])
    if num < 150000:
        raise TraceError("PostgreSQL 15 or newer is required (pg_walinspect).")
    if not obs.execute("select rolsuper from pg_roles where rolname = current_user").fetchone()[0]:
        raise TraceError("Connect as a superuser: pageinspect, auto_explain and the stage "
                         "statistics are superuser-only. Use a local or throwaway instance.")
    for ext in EXTENSIONS:
        try:
            obs.execute(f"create extension if not exists {ext}")
        except psycopg.Error as e:
            raise TraceError(f"Cannot create extension {ext}: {e}. Install the contrib package.")


def _traced(conn, sql, extended=False):
    """Execute with libpq protocol tracing on. Returns (cursor, error, messages, ms).

    extended sends Parse, Bind, Describe, Execute and Sync in one round trip,
    as most drivers do; otherwise one simple Query message."""
    cur = err = None
    with tempfile.TemporaryFile() as f:
        try:
            conn.pgconn.trace(f.fileno())
            tracing = True
        except Exception:  # noqa: BLE001  psycopg only supports PQtrace on Linux
            tracing = False
        t0 = time.perf_counter()
        try:
            # psycopg uses the extended protocol for binary results
            cur = conn.execute(sql, binary=True) if extended else conn.execute(sql)
        except psycopg.Error as e:
            err = e
        elapsed = (time.perf_counter() - t0) * 1000
        if tracing:
            conn.pgconn.untrace()
        f.seek(0)
        text = f.read().decode("utf-8", "replace")
    return cur, err, parse_trace(text), round(elapsed, 3)


def _safe(fn, *args, **kwargs):
    """A layer that fails must not take the other layers down with it."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e).strip()}"}


def run_trace(dsn, sql, *, commit=True, cold=False, checkpoint=False, os_trace=False, max_pages=8,
              protocol="extended", show_trees=True, lock_wait_ms=0, with_sql=None, deadlock_sql=None):
    sql = sql.strip()
    if not single_statement(sql):
        raise TraceError("One statement at a time, please.")

    obs = psycopg.connect(dsn, autocommit=True, application_name="pgtrail-observer")
    tgt = psycopg.connect(dsn, autocommit=True, application_name="pgtrail-target")
    _prepare_server(obs)
    rels = Rels(obs)
    pid = tgt.info.backend_pid
    notices = []
    # Diagnostic objects are only valid inside the callback, so copy now.
    tgt.add_notice_handler(
        lambda d: notices.append((d.severity, d.message_primary, d.message_detail))
    )

    # Unless --cold, let the backend load its catalog caches first, so the trace
    # shows the statement and not the one-off cost of a brand new connection.
    if not cold:
        try:
            tgt.execute("BEGIN")
            tgt.execute("EXPLAIN " + sql)
        except psycopg.Error:
            pass
        finally:
            tgt.execute("ROLLBACK")
    tgt.execute("LOAD 'auto_explain'")
    for setting in SESSION + (TREES if show_trees else ()):
        tgt.execute("SET " + setting)

    settings = dict(
        obs.execute(
            "select name, setting || coalesce(' ' || unit, '') from pg_settings where name = any(%s)",
            (list(SETTINGS_OF_INTEREST),),
        ).fetchall()
    )
    block_size = int(settings.get("block_size", "8192").split()[0])
    tls = obs.execute("select version from pg_stat_ssl where pid = %s and ssl", (pid,)).fetchone()
    trace = {
        "meta": {
            "sql": sql,
            "server_version": obs.execute("show server_version").fetchone()[0].split()[0],
            "backend_pid": pid,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cold_backend": cold,
            "tls": tls[0] if tls else None,
            "settings": settings,
            "protocol": protocol,
            "trees": show_trees,
            "data_directory": rels.data_directory,
        }
    }

    # Warm the observer too, so its own catalog lookups stay out of the diff.
    buffers.snapshot(obs)
    locks.collect(obs, pid)

    blocker = None
    if lock_wait_ms or with_sql:
        try:
            blocker = contention.OtherSession(dsn, obs, sql, lock_wait_ms, with_sql, deadlock_sql, commit)
        except (psycopg.Error, ValueError) as e:
            trace["lock_wait"] = {"error": "no other session: " + str(e).strip().splitlines()[0]}
    begun = False
    if blocker and deadlock_sql:
        # this session's half of the cycle, inside the transaction but before the
        # measuring starts, so its rows and WAL are not counted as the statement's
        tgt.execute("BEGIN")
        begun = True
        try:
            trace["meta"]["deadlock_first"] = {"sql": deadlock_sql,
                                               "status": contention.run_first(tgt, deadlock_sql)}
        except psycopg.Error as e:
            raise TraceError(f"--deadlock statement failed: {str(e).strip().splitlines()[0]}")

    strace = None
    if os_trace:
        roles = {pid: "backend"}
        roles.update(
            {p: t for p, t in obs.execute(
                "select pid, backend_type from pg_stat_activity where backend_type = any(%s)",
                (list(BACKGROUND),))}
        )
        strace = syscalls.Strace(roles)
        try:
            strace.start()
        except syscalls.StraceUnavailable as e:
            trace["syscalls"] = {"error": str(e)}
            strace = None

    start_lsn = obs.execute("select pg_current_wal_insert_lsn()").fetchone()[0]
    before = buffers.snapshot(obs)
    if not begun:
        tgt.execute("BEGIN")
    notices.clear()

    # ---- the statement itself -------------------------------------------
    phases = []
    p0 = syscalls.now_seconds()
    if blocker:
        blocker.start(pid)
    cur, err, messages, elapsed = _traced(tgt, sql, extended=protocol == "extended")
    if blocker:
        trace["lock_wait"] = blocker.result(elapsed, str(err).strip() if err else None)
    phases.append(("statement", p0, syscalls.now_seconds()))
    statement_notices = list(notices)

    after = buffers.snapshot(obs)
    xid = obs.execute(
        "select backend_xid::text from pg_stat_activity where pid = %s", (pid,)
    ).fetchone()[0]
    trace["meta"]["xid"] = xid
    trace["locks"] = _safe(locks.collect, obs, pid)

    result = {"status": None, "columns": [], "row_count": 0, "sample": [], "error": None}
    if err:
        result["error"] = str(err).strip()
    else:
        result["status"] = cur.statusmessage
        if cur.description:
            rows = cur.fetchall()
            result["columns"] = [c.name for c in cur.description]
            result["row_count"] = len(rows)
            result["sample"] = [[str(v) for v in r] for r in rows[:5]]
    trace["result"] = result
    trace["protocol"] = {"messages": messages, "round_trip_ms": elapsed}
    trace["stages"], trace["plan"] = stages.parse_notices(statement_notices)
    if show_trees:
        trace["trees"] = _safe(trees.collect, statement_notices, obs)

    # ---- end of transaction ---------------------------------------------
    committed = commit and not err
    p0 = syscalls.now_seconds()
    _, _, end_messages, end_elapsed = _traced(tgt, "COMMIT" if committed else "ROLLBACK")
    phases.append(("commit" if committed else "rollback", p0, syscalls.now_seconds()))
    trace["end"] = {
        "command": "COMMIT" if committed else "ROLLBACK",
        "messages": end_messages,
        "round_trip_ms": end_elapsed,
    }
    if trace["plan"] is None:
        # With the extended protocol inside a transaction, a statement that
        # returns rows keeps its portal open until the transaction ends, and
        # auto_explain reports the plan only when the portal closes.
        trace["plan"] = stages.parse_notices(notices[len(statement_notices):])[1]

    end_lsn = wal.wait_flushed(obs)
    trace["wal"] = _safe(wal.collect, obs, rels, start_lsn, end_lsn, xid)
    modified = []
    if "error" not in trace["wal"]:
        modified = [b for r in trace["wal"]["records"] for b in r["blocks"]]
        trace["pages"] = _safe(pages.collect, obs, trace["wal"], xid, committed, max_pages)
    trace["buffers"] = _safe(buffers.diff, rels, before, after, [b["key"] for b in modified])

    # ---- what is left for the background processes ----------------------
    if "error" not in trace["buffers"]:
        dirty_keys = [p["key"] for g in trace["buffers"]["relations"]
                      for p in g["pages"] if p["dirtied"] or p["modified"]]
        aftermath = {"dirtied": len(dirty_keys),
                     "still_dirty": buffers.still_dirty(obs, dirty_keys)}
        if checkpoint:
            p0, t0 = syscalls.now_seconds(), time.perf_counter()
            obs.execute("CHECKPOINT")
            aftermath["checkpoint_ms"] = round((time.perf_counter() - t0) * 1000, 3)
            phases.append(("checkpoint", p0, syscalls.now_seconds()))
            aftermath["dirty_after_checkpoint"] = buffers.still_dirty(obs, dirty_keys)
        trace["aftermath"] = aftermath

    if strace:
        text = strace.stop()
        notice_sizes = {
            size
            for m in trace["protocol"]["messages"] + trace["end"]["messages"]
            if m["type"] == "NoticeResponse"
            for size in m["wire_sizes"]
        }
        ours = {(b["relation"], b["fork"], b["block"]) for b in modified}
        trace["syscalls"] = _safe(
            lambda: {"events": syscalls.parse(text, strace.roles, phases, rels, block_size,
                                              ours, notice_sizes, bool(tls))}
        )

    # last, so the extra planning cannot show up in anything measured above
    trace["plans"] = _safe(plans.collect, obs, sql)
    trace["statistics"] = _safe(statistics.collect, obs, trace["plan"])
    trace["btree"] = _safe(btree.collect, obs, trace["plan"], block_size)

    tgt.close()
    obs.close()
    return trace
