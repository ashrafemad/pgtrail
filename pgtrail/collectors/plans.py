"""Layer 3b: the plans the planner did not pick.

PostgreSQL keeps only the cheapest plan and throws the other candidates away,
so they are recovered the way a DBA would: plan the statement again with the
winner's methods switched off (enable_* = off), and repeat. Each is EXPLAIN
only, so nothing runs, and the costs are the planner's estimates.
"""
import json

import psycopg

# node type → the planner setting that discourages it
SETTINGS = {
    "Seq Scan": "enable_seqscan",
    "Index Scan": "enable_indexscan",
    "Index Only Scan": "enable_indexonlyscan",
    "Bitmap Heap Scan": "enable_bitmapscan",
    "Tid Scan": "enable_tidscan",
    "Nested Loop": "enable_nestloop",
    "Hash Join": "enable_hashjoin",
    "Merge Join": "enable_mergejoin",
    "Memoize": "enable_memoize",
    "Material": "enable_material",
    "Sort": "enable_sort",
    "Incremental Sort": "enable_incremental_sort",
}
# a switched-off method is not forbidden: the planner adds this to its cost,
# so a total this high means there was no other way
DISABLE_COST = 1.0e10
MAX_PLANS = 5


def _nodes(node, depth=0):
    yield node, depth
    for child in node.get("Plans", []):
        yield from _nodes(child, depth + 1)


def _kind(node):
    if node["Node Type"] == "ModifyTable":
        return node.get("Operation", "ModifyTable")
    if node["Node Type"] == "Aggregate":
        return {"Hashed": "HashAggregate", "Sorted": "GroupAggregate"}.get(node.get("Strategy"), "Aggregate")
    return node["Node Type"]


def _setting(node):
    return "enable_hashagg" if _kind(node) == "HashAggregate" else SETTINGS.get(node["Node Type"])


def _label(node):
    text = _kind(node)
    if node.get("Index Name"):
        text += f" using {node['Index Name']}"
    if node.get("Relation Name"):
        text += f" on {node['Relation Name']}"
    return text


def _explain(conn, sql, off):
    conn.execute("BEGIN")
    try:
        for setting in off:
            conn.execute(f"SET LOCAL {setting} = off")
        doc = conn.execute("EXPLAIN (FORMAT JSON) " + sql).fetchone()[0]
    finally:
        conn.execute("ROLLBACK")
    doc = json.loads(doc) if isinstance(doc, str) else doc
    return doc[0]["Plan"]


JOINS = ("Nested Loop", "Hash Join", "Merge Join")
MAX_EXPLAINS = 30


def _entry(plan, off, chosen_total):
    methods = [n for n, _ in _nodes(plan) if _setting(n)]
    # the join method says most about a plan, then the first scan
    first = next((n for n in methods if n["Node Type"] in JOINS),
                 next((n for n in methods if "Scan" in n["Node Type"]), methods[0] if methods else plan))
    return {
        "summary": " + ".join(_label(n) for n in methods) or _label(plan),
        "method": _kind(first),
        "target": first.get("Index Name") or first.get("Relation Name") or "",
        "startup_cost": plan["Startup Cost"],
        "total_cost": plan["Total Cost"],
        "rows": plan["Plan Rows"],
        "ratio": round(plan["Total Cost"] / chosen_total, 2) if chosen_total else 1.0,
        "disabled": list(off),
        "lines": [f"{'   ' * d}{'-> ' if d else ''}{_label(n)}  "
                  f"(cost {n['Startup Cost']:.2f}..{n['Total Cost']:.2f}, rows {n['Plan Rows']})"
                  for n, d in _nodes(plan)],
        "chosen": not off,
    }


def _signature(plan):
    return tuple(_label(n) for n, _ in _nodes(plan))


def collect(conn, sql):
    try:
        chosen = _explain(conn, sql, [])
    except psycopg.Error as e:
        return {"considered": [], "note": "no plan to compare: " + str(e).strip().splitlines()[0]}
    found = {_signature(chosen): (chosen, [])}
    tried, explains = set(), 0
    # switch off one of the winner's methods at a time, then one more from each
    # runner-up; switching off everything at once would also rule out steps
    # with no alternative (an ORDER BY needs its Sort)
    frontier = [(chosen, [])]
    for _ in range(2):
        next_frontier = []
        for plan, off in frontier:
            for setting in sorted({_setting(n) for n, _ in _nodes(plan) if _setting(n)} - set(off)):
                attempt = tuple(sorted(off + [setting]))
                if attempt in tried or explains >= MAX_EXPLAINS:
                    continue
                tried.add(attempt)
                explains += 1
                alt = _explain(conn, sql, list(attempt))
                if alt["Total Cost"] >= DISABLE_COST:
                    continue            # only a switched-off method was left for some step
                signature = _signature(alt)
                if signature not in found:
                    found[signature] = (alt, list(attempt))
                    next_frontier.append((alt, list(attempt)))
        frontier = next_frontier
    total = chosen["Total Cost"]
    others = sorted((v for k, v in found.items() if k != _signature(chosen)),
                    key=lambda v: v[0]["Total Cost"])
    considered = [_entry(chosen, [], total)] + [
        _entry(plan, off, total) for plan, off in others[: MAX_PLANS - 1]]
    note = None
    if len(considered) == 1:
        note = "only one way to run this: there was nothing to choose between"
    elif len(others) > MAX_PLANS - 1:
        note = f"{len(others) - MAX_PLANS + 1} more plan(s) found, not shown"
    return {"considered": considered, "note": note}
