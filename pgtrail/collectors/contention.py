"""Layer 3b: another session at the same time (--lock-wait, --with, --deadlock).

A second session, the other one, does its work before the statement is sent
and keeps its transaction open:

  --lock-wait MS      it locks the statement's first table in ACCESS EXCLUSIVE
                      mode and lets go after MS milliseconds
  --with SQL          it runs SQL instead, and ends its transaction after the
                      same time (500 ms unless --lock-wait says otherwise)
  --deadlock SQL      with --with: this session runs SQL first; the other runs
                      --with, then SQL too (and waits for this session); then the
                      traced statement waits for the other. A cycle, which
                      PostgreSQL's detector breaks after deadlock_timeout.

While the statement waits, the observer records what each session waits on,
as the server itself reports it in pg_stat_activity and pg_locks.
"""
import json
import threading
import time

import psycopg
from psycopg import sql as pgsql

DEFAULT_HOLD_MS = 500
_WAITING = """
    select a.wait_event_type, a.wait_event, pg_blocking_pids(a.pid),
           l.locktype, l.mode, coalesce(l.relation::regclass::text, l.transactionid::text)
    from pg_stat_activity a
    left join pg_locks l on l.pid = a.pid and not l.granted
    where a.pid = %s
"""
_HELD = """
    select locktype, mode, coalesce(relation::regclass::text, transactionid::text, virtualxid)
    from pg_locks where pid = %s and granted and locktype in ('relation', 'transactionid', 'tuple')
    order by locktype, 3
"""


def first_relation(conn, statement):
    """The first table in the statement's plan: the one it will open first."""
    doc = conn.execute("EXPLAIN (FORMAT JSON) " + statement).fetchone()[0]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    queue = [doc[0]["Plan"]]
    while queue:
        node = queue.pop(0)
        if node.get("Relation Name"):
            return node["Relation Name"]
        queue.extend(node.get("Plans", []))
    return None


def _waiting(obs, pid, since):
    row = obs.execute(_WAITING, (pid,)).fetchone()
    if not row:
        return None
    return {"at_ms": round((time.perf_counter() - since) * 1000, 1),
            "wait_event_type": row[0], "wait_event": row[1], "blocking_pids": row[2],
            "locktype": row[3], "mode": row[4], "target": row[5]}


class OtherSession:
    def __init__(self, dsn, obs, statement, hold_ms=0, with_sql=None, deadlock_sql=None, commit=True):
        self.obs, self.commit = obs, commit
        self.hold_ms = hold_ms or DEFAULT_HOLD_MS
        self.with_sql, self.deadlock_sql = with_sql, deadlock_sql
        self.scenario = "deadlock" if deadlock_sql else "with" if with_sql else "lock"
        self.relation = None
        if self.scenario == "lock":
            self.relation = first_relation(obs, statement)
            if not self.relation:
                raise ValueError("the statement reads no table, so there is nothing to block")
            self.other_sql = f"LOCK TABLE {self.relation} IN ACCESS EXCLUSIVE MODE"
        else:
            self.other_sql = with_sql
        self.conn = psycopg.connect(dsn, application_name="pgtrail-other")
        self.pid = self.conn.info.backend_pid
        # a mistake in --with must not hang the trace forever
        self.conn.execute("SET lock_timeout = '10s'")
        if self.scenario == "lock":
            self.conn.execute(pgsql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE")
                              .format(pgsql.Identifier(self.relation)))
            self.status = "LOCK TABLE"
        else:
            self.status = self.conn.execute(with_sql).statusmessage
        self.held = [list(r) for r in obs.execute(_HELD, (self.pid,))]
        self.deadlock_timeout = obs.execute("show deadlock_timeout").fetchone()[0]
        self.seen = self.other_seen = None
        self.sent = self.released = None
        self.ended = None
        self.error = self.error_detail = None
        self._threads = []

    def start(self, target_pid):
        """Call right before the traced statement is sent."""
        if self.scenario == "deadlock":
            # the other session asks for what this session holds, and waits for it
            t = threading.Thread(target=self._second_statement, daemon=True)
            t.start()
            self._threads.append(t)
            time.sleep(0.2)                 # so it is surely waiting before this statement goes
        self.sent = time.perf_counter()
        watch_at = (self._timeout_ms() / 2) if self.scenario == "deadlock" else self.hold_ms / 2
        w = threading.Thread(target=self._watch, args=(target_pid, watch_at), daemon=True)
        w.start()
        self._threads.append(w)
        if self.scenario != "deadlock":
            h = threading.Thread(target=self._hold, daemon=True)
            h.start()
            self._threads.append(h)

    def _timeout_ms(self):
        value = self.deadlock_timeout.strip()
        for unit, factor in (("ms", 1), ("s", 1000), ("min", 60000)):
            if value.endswith(unit):
                return float(value[: -len(unit)]) * factor
        return float(value)

    def _watch(self, target_pid, after_ms):
        time.sleep(after_ms / 1000)
        try:
            self.seen = _waiting(self.obs, target_pid, self.sent)
            self.other_seen = _waiting(self.obs, self.pid, self.sent)
        except psycopg.Error as e:
            self.seen = {"error": str(e).strip()}

    def _hold(self):
        time.sleep(max(0.0, self.hold_ms / 1000 - (time.perf_counter() - self.sent)))
        self.ended = "COMMIT" if self.commit else "ROLLBACK"
        self.conn.execute(self.ended)
        self.released = time.perf_counter()  # the locks are free once this returns
        self.conn.close()

    def _second_statement(self):
        try:
            self.conn.execute(self.deadlock_sql)
        except psycopg.Error as e:
            self.error = (e.diag.message_primary or str(e)).strip()
            self.error_detail = (e.diag.message_detail or "").strip()
        self.released = time.perf_counter()
        self.ended = "cancelled" if self.error else "ROLLBACK"
        self.conn.execute("ROLLBACK")       # a demo: leave the data as it was
        self.conn.close()

    def result(self, round_trip_ms, statement_error=None):
        for t in self._threads:
            t.join(timeout=60)
        blocked = bool(self.seen and self.seen.get("wait_event_type") == "Lock")
        waited = 0.0
        if blocked and self.released and self.sent:
            waited = min((self.released - self.sent) * 1000, round_trip_ms)
        elif blocked:
            waited = round_trip_ms
        out = {
            "scenario": self.scenario,
            "other_pid": self.pid,
            "other_sql": self.other_sql,
            "other_status": self.status,
            "other_held": self.held,
            "other_end": self.ended,
            "held_for_ms": self.hold_ms,
            "blocked": blocked,
            "waited_ms": round(waited, 3),
            "seen_while_waiting": self.seen,
            "other_seen_while_waiting": self.other_seen,
            # the --lock-wait story
            "relation": self.relation,
            "mode": "AccessExclusiveLock" if self.scenario == "lock" else None,
            "blocker_pid": self.pid,
        }
        if self.scenario == "deadlock":
            mine = statement_error and "deadlock detected" in statement_error
            out["deadlock_sql"] = self.deadlock_sql
            out["deadlock"] = {
                "detected": bool(self.error and "deadlock" in self.error) or bool(mine),
                "victim": "this statement" if mine else "the other session" if self.error else None,
                "message": self.error or (statement_error.splitlines()[0] if mine else None),
                "detail": self.error_detail,
                "timeout": self.deadlock_timeout,
            }
        return out


def run_first(conn, sql_text):
    """--deadlock: this session's first statement, before the trace starts."""
    conn.execute("SET lock_timeout = '10s'")
    status = conn.execute(sql_text).statusmessage
    conn.execute("SET lock_timeout = 0")
    return status
