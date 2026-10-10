"""Layer 3: the lock manager. What the backend holds once the statement ran."""

_WHY = {
    "AccessShareLock": "reading; only blocks DROP / ALTER / VACUUM FULL",
    "RowShareLock": "SELECT ... FOR UPDATE/SHARE",
    "RowExclusiveLock": "modifying rows; other readers and writers still allowed",
    "ShareUpdateExclusiveLock": "VACUUM / ANALYZE / CREATE INDEX CONCURRENTLY",
    "ShareLock": "CREATE INDEX; blocks writers",
    "ShareRowExclusiveLock": "CREATE TRIGGER and some ALTER TABLE forms",
    "ExclusiveLock": "held on its own transaction id so others can wait for it",
    "AccessExclusiveLock": "DDL; blocks everything, including plain SELECT",
}


def collect(obs, pid):
    rows = obs.execute(
        """
        select locktype,
               case when relation is not null and database in (0, (select oid
                    from pg_database where datname = current_database()))
                    then relation::regclass::text end,
               mode, granted, fastpath, transactionid::text, virtualxid,
               classid::regclass::text, objid
        from pg_locks where pid = %s
        order by locktype, 2
        """,
        (pid,),
    ).fetchall()
    out = []
    for locktype, rel, mode, granted, fastpath, xid, vxid, classid, objid in rows:
        if rel and rel.isdigit():
            # created by this still-open transaction, so the observer cannot see its name yet
            target = f"new relation, oid {rel}"
        elif rel:
            target = rel
        elif xid:
            target = f"xid {xid}"
        elif vxid:
            target = f"virtual xid {vxid}"
        elif classid:
            target = f"{classid} entry, oid {objid}"
        else:
            target = locktype
        out.append(
            {
                "locktype": locktype,
                "target": target,
                "mode": mode,
                "granted": granted,
                "fastpath": fastpath,
                "why": _WHY.get(mode, ""),
            }
        )
    return out
