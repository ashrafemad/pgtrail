"""Layer 2d: the statistics the planner estimated from.

For every column the executed plan filters, joins, groups or sorts on, the
planner's estimate came from pg_stats (distinct values, most common values,
histogram, correlation) and pg_class (rows and pages). Read after the trace.
"""
import re

CONDITIONS = ("Filter", "Index Cond", "Recheck Cond", "Hash Cond", "Merge Cond", "Join Filter")
KEYS = ("Group Key", "Sort Key")
_IDENT = re.compile(r'(?:"?([A-Za-z_]\w*)"?\.)?"?([A-Za-z_]\w*)"?')


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _columns(plan):
    """{table: {column}} named in the plan's conditions and keys."""
    nodes = list(_nodes(plan))
    aliases = {}
    for n in nodes:
        if n.get("Relation Name"):
            aliases[n["Relation Name"]] = n["Relation Name"]
            aliases[n.get("Alias", n["Relation Name"])] = n["Relation Name"]
    wanted = {}
    for n in nodes:
        texts = [n[k] for k in CONDITIONS if n.get(k)] + [k for key in KEYS for k in n.get(key, [])]
        for text in texts:
            for qualifier, column in _IDENT.findall(text):
                table = aliases.get(qualifier) if qualifier else n.get("Relation Name")
                if table:
                    wanted.setdefault(table, set()).add(column)
    return wanted


def collect(conn, plan_doc):
    plan = (plan_doc or {}).get("Plan")
    if not plan:
        return []
    out = []
    for table, columns in sorted(_columns(plan).items()):
        found = conn.execute(
            "select reltuples::bigint, relpages from pg_class where oid = to_regclass(%s)", (table,)
        ).fetchone()
        if not found:
            continue
        rows, pages = found
        stats = conn.execute(
            """
            select attname, null_frac, n_distinct, most_common_vals::text::text[],
                   most_common_freqs, histogram_bounds::text::text[], correlation
            from pg_stats
            where tablename = %s and attname = any(%s)
              and schemaname = (select relnamespace::regnamespace::text
                                from pg_class where oid = to_regclass(%s))
            order by attname
            """,
            (table, sorted(columns), table),
        ).fetchall()
        described = []
        for name, null_frac, n_distinct, mcv, freqs, hist, corr in stats:
            distinct = round(-n_distinct * rows) if n_distinct < 0 else round(n_distinct)
            described.append({
                "column": name,
                "null_frac": round(null_frac, 4),
                "n_distinct": n_distinct,
                "distinct": distinct,
                "unique": n_distinct == -1,
                "most_common": [[v, round(f, 4)] for v, f in zip((mcv or [])[:5], (freqs or [])[:5])],
                "most_common_count": len(mcv or []),
                "histogram": {"buckets": len(hist) - 1, "min": hist[0], "max": hist[-1]} if hist else None,
                "correlation": round(corr, 3) if corr is not None else None,
            })
        out.append({"table": table, "rows": rows, "pages": pages, "columns": described,
                    "analyzed": bool(described)})
    return out
