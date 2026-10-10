"""Layer 6b: the B-tree indexes the plan used, read with pageinspect.

For each index: its height, root and size. For a lookup on a single integer
key (id = 42), the descent is repeated by hand, page by page from the root to
the leaf, the way the index scan did it: on an inner page the search follows
the last key below the value searched for; on the leaf it finds the entry
pointing at the row. Read after the trace, so nothing measured changes.
"""
import re

from psycopg import sql

INDEX_SCANS = ("Index Scan", "Index Only Scan", "Bitmap Index Scan")
INT_SIZES = {"smallint": 2, "integer": 4, "bigint": 8}
_EQUALS = re.compile(r"\(\s*(?:\w+\.)?\"?(\w+)\"?\s*=\s*'?(-?\d+)'?(?:::\w+)?\s*\)")
MAX_LEVELS = 10


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _key(data, size):
    """An integer key from the item's bytes; None for an empty key (minus infinity)."""
    raw = bytes(int(b, 16) for b in (data or "").split())
    if len(raw) < size:
        return None
    return int.from_bytes(raw[:size], "little", signed=True)


def _block(tid):
    m = re.match(r"\((\d+),(\d+)\)", tid or "")
    return int(m[1]) if m else None


HOT_UPDATED = 0x4000                   # t_infomask2: the next version is on the same page


def _hot_chain(conn, table, tid, current):
    """Follow a heap-only-tuple chain from where an index entry points: through
    redirects and same-page newer versions. Returns the steps if it reaches a
    current row version, else None."""
    m = re.match(r"\((\d+),(\d+)\)", tid or "")
    if not m:
        return None
    block, lp = int(m[1]), int(m[2])
    items = {r[0]: r[1:] for r in conn.execute(
        "select lp, lp_flags, lp_off, t_ctid::text, t_infomask2 from heap_page_items(get_raw_page(%s, %s))",
        (table, block))}
    steps = []
    for _ in range(50):
        here = f"({block},{lp})"
        if here in current:
            return steps
        flags, off, ctid, infomask2 = items.get(lp, (0, 0, None, 0))
        if flags == 2:                      # a redirect left behind by pruning
            lp = off
        elif ctid and infomask2 and infomask2 & HOT_UPDATED:
            nxt = re.match(r"\((\d+),(\d+)\)", ctid)
            if not nxt or int(nxt[1]) != block:
                return None
            lp = int(nxt[2])
        else:
            return None
        steps.append(f"({block},{lp})")
    return None


def _descend(conn, index, root, value, size, current, table):
    path, block = [], root
    for _ in range(MAX_LEVELS):
        kind, items, right, level = conn.execute(
            "select type, live_items, btpo_next, btpo_level from bt_page_stats(%s, %s)", (index, block)
        ).fetchone()
        rows = conn.execute(
            "select itemoffset, ctid::text, data, htid::text, tids::text[] from bt_page_items(%s, %s)",
            (index, block)).fetchall()
        # on every page but the rightmost of its level, item 1 is the high key
        entries = rows if right == 0 else rows[1:]
        step = {"block": block, "level": level, "items": items, "kind": {"r": "root", "i": "inner",
                                                                           "l": "leaf"}.get(kind, kind)}
        if level == 0:
            # an UPDATE that moves a row adds an entry; the old ones stay until VACUUM.
            # Equal keys are ordered by row address and may continue on the next leaf.
            tids, leaf, more, posting = [], block, [], 0
            for _ in range(MAX_LEVELS):
                for r in entries:
                    if _key(r[2], size) == value:
                        # a deduplicated entry carries a list of row addresses
                        tids += r[4] if r[4] else [r[3] or r[1]]
                        posting += 1 if r[4] else 0
                if right == 0 or not entries or _key(entries[-1][2], size) != value:
                    break
                leaf, more = right, more + [right]
                _, _, right, _ = conn.execute(
                    "select type, live_items, btpo_next, btpo_level from bt_page_stats(%s, %s)", (index, leaf)
                ).fetchone()
                rows = conn.execute("select itemoffset, ctid::text, data, htid::text, tids::text[] from "
                                    "bt_page_items(%s, %s)", (index, leaf)).fetchall()
                entries = rows if right == 0 else rows[1:]
            step["entries"] = []
            for tid in tids:
                chain = _hot_chain(conn, table, tid, current)
                step["entries"].append({"row": tid, "current": chain is not None,
                                        "hot_chain": chain or []})
            step["next_leaves"] = more
            step["posting_lists"] = posting
            hit = next((e for e in step["entries"] if e["current"]), None)
            step["found"] = (hit["hot_chain"][-1] if hit["hot_chain"] else hit["row"]) if hit else None
            path.append(step)
            return path
        below = [r for r in entries if _key(r[2], size) is None or _key(r[2], size) < value]
        chosen = below[-1] if below else entries[0]
        # every branch of this page and the lowest key below it, to draw the page
        step["children"] = [[_block(r[1]), _key(r[2], size)] for r in entries]
        step["child"] = _block(chosen[1])
        step["key"] = _key(chosen[2], size)
        path.append(step)
        block = step["child"]
    return path


def collect(conn, plan_doc, block_size):
    plan = (plan_doc or {}).get("Plan")
    if not plan:
        return []
    out, seen = [], set()
    for node in _nodes(plan):
        index = node.get("Index Name")
        if node.get("Node Type") not in INDEX_SCANS or not index or index in seen:
            continue
        seen.add(index)
        meta = conn.execute("select root, level from bt_metap(%s)", (index,)).fetchone()
        if not meta:
            continue
        root, level = meta
        pages = conn.execute("select pg_relation_size(to_regclass(%s)) / %s", (index, block_size)).fetchone()[0]
        column, key_type, columns, table = conn.execute(
            """
            select a.attname, a.atttypid::regtype::text, i.indnatts, i.indrelid::regclass::text
            from pg_index i join pg_attribute a on a.attrelid = i.indexrelid and a.attnum = 1
            where i.indexrelid = to_regclass(%s)
            """, (index,)).fetchone()
        entry = {"index": index, "table": table, "height": level + 1, "root": root,
                 "pages": pages, "column": column, "key_type": key_type, "lookup": None, "path": []}
        m = _EQUALS.search(node.get("Index Cond") or "")
        size = INT_SIZES.get(key_type)
        if m and m[1] == column and size and columns == 1:
            entry["lookup"] = int(m[2])
            # where the visible row versions are now, to tell current entries from old ones
            current = {r[0] for r in conn.execute(
                sql.SQL("select ctid::text from {} where {} = %s").format(
                    sql.SQL(table), sql.Identifier(column)), (int(m[2]),))}
            entry["path"] = _descend(conn, index, root, int(m[2]), size, current, table)
        out.append(entry)
    return out
