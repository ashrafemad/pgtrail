"""Layer 6: the pages themselves. Uses pageinspect on every block the
statement's WAL records point at, and picks out what this transaction did
by matching xmin / xmax against its transaction id."""
from psycopg import sql

HEAP_XMAX_LOCK_ONLY = 0x0080
HEAP_HOT_UPDATED = 0x4000
HEAP_ONLY_TUPLE = 0x8000
_LP_FLAGS = {0: "unused", 1: "normal", 2: "redirect", 3: "dead"}
_BT_TYPES = {"l": "leaf", "i": "internal", "r": "root", "d": "deleted", "e": "ignored"}


def _header(obs, rel, block):
    lsn, lower, upper, special, size = obs.execute(
        "select lsn::text, lower, upper, special, pagesize "
        "from page_header(get_raw_page(%s, 'main', %s))",
        (rel, block),
    ).fetchone()
    return {
        "lsn": lsn,
        "lower": lower,
        "upper": upper,
        "special": special,
        "page_size": size,
        "free_bytes": upper - lower,
        "line_pointers": (lower - 24) // 4,
    }


def _heap(obs, rel, block, xid, committed):
    items = obs.execute(
        "select lp, lp_flags, lp_off, lp_len, t_xmin::text, t_xmax::text, t_ctid::text, "
        "t_infomask, t_infomask2 from heap_page_items(get_raw_page(%s, 'main', %s))",
        (rel, block),
    ).fetchall()
    changes, new_tids = [], []
    states = {}
    for lp, flags, off, length, xmin, xmax, ctid, mask, mask2 in items:
        states[_LP_FLAGS.get(flags, "?")] = states.get(_LP_FLAGS.get(flags, "?"), 0) + 1
        if xid is None or flags != 1:
            continue
        tid = f"({block},{lp})"
        base = {"lp": lp, "offset": off, "length": length, "xmin": xmin, "xmax": xmax}
        if xmin == str(xid):
            row = None
            if committed and xmax != str(xid):
                got = obs.execute(
                    sql.SQL("select t::text from {} t where ctid = %s::tid").format(sql.SQL(rel)),
                    (tid,),
                ).fetchone()
                row = got[0] if got else None
            new_tids.append(tid)
            changes.append(
                dict(base, action="created", row=row, heap_only=bool(mask2 & HEAP_ONLY_TUPLE))
            )
        if xmax == str(xid):
            if mask & HEAP_XMAX_LOCK_ONLY:
                changes.append(dict(base, action="locked"))
            elif ctid != tid:
                changes.append(
                    dict(base, action="superseded", new_version=ctid,
                         hot=bool(mask2 & HEAP_HOT_UPDATED))
                )
            else:
                changes.append(dict(base, action="deleted"))
    return {"line_pointer_states": states, "changes": changes}, new_tids


def _btree(obs, rel, block, new_tids):
    if block == 0:
        root, level = obs.execute("select root, level from bt_metap(%s)", (rel,)).fetchone()
        return {"node": "metapage", "root_block": root, "tree_height": level + 1, "entries": []}
    kind, live, dead, free, level = obs.execute(
        "select type::text, live_items, dead_items, free_size, btpo_level "
        "from bt_page_stats(%s, %s)",
        (rel, block),
    ).fetchone()
    entries = []
    for off, htid, data, tids in obs.execute(
        "select itemoffset, htid::text, data, tids::text[] from bt_page_items(%s, %s)",
        (rel, block),
    ):
        hit = [t for t in ([htid] + list(tids or [])) if t in new_tids]
        if hit:
            entries.append({"item": off, "points_to": hit[0], "key_bytes": data})
    return {
        "node": _BT_TYPES.get(kind, kind),
        "level": level,
        "live_items": live,
        "dead_items": dead,
        "free_bytes": free,
        "entries": entries,
    }


def collect(obs, wal, xid, committed, max_pages=8):
    seen, heap_blocks, index_blocks = set(), [], []
    for rec in wal["records"]:
        for b in rec["blocks"]:
            key = (b["relation"], b["block"])
            if key in seen or b["fork"] != "main" or not b["inspectable"]:
                continue
            seen.add(key)
            if b["am"] == "heap":
                heap_blocks.append(b)
            elif b["am"] == "btree":
                index_blocks.append(b)
    out, new_tids = [], []
    total = len(heap_blocks) + len(index_blocks)
    for b in heap_blocks[:max_pages]:
        body, tids = _heap(obs, b["relation"], b["block"], xid, committed)
        new_tids += tids
        out.append(dict(relation=b["relation"], block=b["block"], type="heap",
                        header=_header(obs, b["relation"], b["block"]), **body))
    for b in index_blocks[: max(0, max_pages - len(out))]:
        out.append(dict(relation=b["relation"], block=b["block"], type="btree",
                        header=_header(obs, b["relation"], b["block"]),
                        **_btree(obs, b["relation"], b["block"], new_tids)))
    return {"pages": out, "not_shown": total - len(out)}
