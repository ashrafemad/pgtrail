"""Layer 5: the write-ahead log. Reads the records the statement produced."""
import re
import time

_FORK_NUMBERS = {"main": 0, "fsm": 1, "vm": 2, "init": 3}
_BLK = re.compile(r"blkref #(\d+): rel (\d+)/(\d+)/(\d+) fork (\w+) blk (\d+)( \(FPW\))?")


def wait_flushed(obs, timeout=2.0):
    """Return an LSN up to which WAL is on disk and covers what we just did."""
    target = obs.execute("select pg_current_wal_insert_lsn()").fetchone()[0]
    deadline = time.monotonic() + timeout
    while True:
        flushed, ok = obs.execute(
            "select pg_current_wal_flush_lsn(), pg_current_wal_flush_lsn() >= %s::pg_lsn",
            (target,),
        ).fetchone()
        if ok or time.monotonic() > deadline:
            return flushed
        time.sleep(0.02)


def collect(obs, rels, start_lsn, end_lsn, xid):
    if obs.execute("select %s::pg_lsn >= %s::pg_lsn", (start_lsn, end_lsn)).fetchone()[0]:
        rows = []
    else:
        rows = obs.execute(
            """
            select start_lsn::text, xid::text, resource_manager, record_type,
                   record_length, fpi_length, description, block_ref, pg_walfile_name(start_lsn)
            from pg_get_wal_records_info(%s::pg_lsn, %s::pg_lsn)
            """,
            (start_lsn, end_lsn),
        ).fetchall()
    records, hidden = [], 0
    for lsn, rxid, rmgr, rtype, length, fpi, desc, block_ref, segment in rows:
        if xid and rxid == str(xid):
            owner = "statement"
        elif rxid == "0":
            owner = "unattributed"  # no xid: pruning, hint bits, background work
        else:
            hidden += 1
            continue
        blocks = []
        for m in _BLK.finditer(block_ref or ""):
            ts, db, node = int(m[2]), int(m[3]), int(m[4])
            rel = rels.by_filenode(db, ts, node)
            blocks.append(
                {
                    "relation": rel["name"],
                    "kind": rel["kind"],
                    "am": rel["am"],
                    "inspectable": rel["inspectable"],
                    "fork": m[5],
                    "block": int(m[6]),
                    "full_page_image": bool(m[7]),
                    # same shape as a pg_buffercache key
                    "key": [db, ts, node, _FORK_NUMBERS.get(m[5], 0), int(m[6])],
                }
            )
        records.append(
            {
                "lsn": lsn,
                "owner": owner,
                "resource_manager": rmgr,
                "record_type": rtype,
                "bytes": length,
                "fpi_bytes": fpi,
                "description": desc,
                "blocks": blocks,
                "file": f"{rels.data_directory}/pg_wal/{segment}",
            }
        )
    mine = [r for r in records if r["owner"] == "statement"]
    return {
        "files": sorted({r["file"] for r in mine}),
        "start_lsn": str(start_lsn),
        "end_lsn": str(end_lsn),
        "records": records,
        "statement_bytes": sum(r["bytes"] for r in mine),
        "statement_records": len(mine),
        "other_transactions_hidden": hidden,
    }
