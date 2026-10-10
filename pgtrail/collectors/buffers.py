"""Layer 4: the buffer manager. Diffs pg_buffercache around the statement.

A snapshot covers every buffer in shared_buffers, so catalog pages, index
pages, free space maps and visibility maps all show up, not just the table
the statement names.
"""
from ..rels import FORKS

_SNAPSHOT = """
    select reldatabase, reltablespace, relfilenode, relforknumber,
           relblocknumber, isdirty, usagecount
    from pg_buffercache where relfilenode is not null
"""


def snapshot(obs):
    return {tuple(r[:5]): (r[5], r[6]) for r in obs.execute(_SNAPSHOT)}


def diff(rels, before, after, modified_keys=()):
    """Group changed buffers by relation and fork.

    modified_keys are the blocks named in the statement's WAL records. They are
    the exact set of modified pages; the dirty flag alone would miss a page
    that was already dirty before the statement touched it.
    """
    modified_keys = {tuple(k) for k in modified_keys}
    groups, cleaned = {}, 0
    for key, (dirty, usage) in after.items():
        prev = before.get(key)
        loaded = prev is None
        dirtied = dirty and (loaded or not prev[0])
        touched = (not loaded) and usage > prev[1]
        if prev and prev[0] and not dirty:
            cleaned += 1
        modified = key in modified_keys
        if not (loaded or dirtied or touched or modified):
            continue
        db, ts, node, fork, block = key
        gkey = (db, ts, node, fork)
        if gkey not in groups:
            fork_name = FORKS.get(fork, str(fork))
            groups[gkey] = dict(
                rels.by_filenode(db, ts, node), fork=fork_name,
                file=rels.file(db, ts, node, fork_name), pages=[]
            )
        page_file = rels.file(db, ts, node, groups[gkey]["fork"], block)
        groups[gkey]["pages"].append(
            {
                "block": block,
                "loaded": loaded,
                "dirtied": dirtied,
                "modified": modified,
                "usage_before": None if loaded else prev[1],
                "usage_after": usage,
                "key": list(key),
                # only past the first 1 GB segment does a block live in another file
                **({"file": page_file} if page_file != groups[gkey]["file"] else {}),
            }
        )
    out = sorted(groups.values(), key=lambda g: (g["system"], g["name"], g["fork"]))
    for g in out:
        g["pages"].sort(key=lambda p: p["block"])
    pages = [p for g in out for p in g["pages"]]
    return {
        "relations": out,
        "summary": {
            "touched": len(pages),
            "loaded": sum(p["loaded"] for p in pages),
            "modified": sum(p["modified"] for p in pages),
            "dirtied_without_wal": sum(p["dirtied"] and not p["modified"] for p in pages),
            "evicted": sum(1 for k in before if k not in after),
            "written_out_by_background": cleaned,
        },
    }


def still_dirty(obs, keys):
    """How many of these buffer keys are dirty right now."""
    now = snapshot(obs)
    return sum(1 for k in keys if now.get(tuple(k), (False, 0))[0])
