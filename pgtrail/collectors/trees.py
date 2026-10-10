"""Layer 2c: the server's own trees for the statement.

With debug_print_parse, debug_print_rewritten and debug_print_plan on, the
backend sends the query tree it built from the SQL, the same tree after the
rewriter, and the plan tree, as LOG notices. The full text is kept; the query
tree is also turned into a list of its nodes with the names behind the
numbers (columns, operators, functions, tables), so a view can say what each
node means. The plan is explained from the executed plan instead (render.py).
"""
import re

TITLES = {"parse tree:": "query", "rewritten parse tree:": "rewritten", "plan:": "plan"}
COMMANDS = {"1": "SELECT", "2": "UPDATE", "3": "INSERT", "4": "DELETE", "5": "MERGE", "6": "UTILITY"}
# node fields kept, to explain the node
KEEP = {"resname", "aliasname", "relid", "opno", "funcid", "aggfnoid", "jointype", "commandType",
        "boolop", "varno", "varattno", "varlevelsup", "rtindex", "rtekind", "consttype",
        "constvalue", "constisnull", "resjunk", "subLinkType", "nulltesttype"}
_NODE = re.compile(r"^(\s*)\{(\w+)")
_FIELD = re.compile(r"^(\s*):([\w.]+)\s*(.*?)\s*$")
_INT_TYPES = {"20": 8, "21": 2, "23": 4, "26": 4}     # int8, int2, int4, oid: bytes to read
# catalog lookups that turn oids in the tree into names
LOOKUPS = {
    "opno": ("opname", "select oid, oprname from pg_operator where oid = any(%s)"),
    "funcid": ("funcname", "select oid, proname from pg_proc where oid = any(%s)"),
    "aggfnoid": ("funcname", "select oid, proname from pg_proc where oid = any(%s)"),
    "relid": ("relname", "select oid, relname from pg_class where oid = any(%s)"),
}


def _parse(text):
    nodes, fields, open_at = [], {}, {}
    for line in text.splitlines():
        m = _NODE.match(line)
        if m:
            indent = len(m[1])
            # inside a list "(" the node sits one level deeper than its field
            field = fields.get(indent - 3) or fields.get(indent - 6)
            parent = open_at.get(indent - 3, open_at.get(indent - 6))
            nodes.append({"name": m[2], "field": field, "depth": indent // 3 - 1,
                          "parent": parent, "attrs": {}})
            open_at[indent] = len(nodes) - 1
            for deeper in [k for k in fields if k >= indent]:
                del fields[deeper]
            continue
        f = _FIELD.match(line)
        if f:
            # plan fields read like plan.targetlist; the last part says the most
            indent, name, value = len(f[1]), f[2].split(".")[-1], f[3]
            fields[indent] = name
            node = open_at.get(indent)
            if node is not None and name in KEEP and value not in ("", "(", "<>"):
                nodes[node]["attrs"][name] = value
    return nodes


def _const(attrs):
    """A small integer or boolean constant's value, from the Datum bytes."""
    if attrs.get("constisnull") == "true":
        return "NULL"
    raw = re.findall(r"-?\d+", attrs.get("constvalue", "").partition("[")[2])
    size = _INT_TYPES.get(attrs.get("consttype"))
    if size and len(raw) >= size:
        data = bytes(int(b) & 0xFF for b in raw[:size])
        return str(int.from_bytes(data, "little", signed=True))
    if attrs.get("consttype") == "16" and raw:
        return "true" if int(raw[0]) else "false"
    return None


def _resolve(nodes, conn):
    """Put names next to the numbers: operators, functions, tables, columns."""
    for field, (label, sql) in LOOKUPS.items():
        oids = sorted({int(n["attrs"][field]) for n in nodes if n["attrs"].get(field, "").isdigit()})
        if oids:
            names = dict(conn.execute(sql, (oids,)).fetchall())
            for n in nodes:
                oid = n["attrs"].get(field, "")
                if oid.isdigit() and int(oid) in names:
                    n["attrs"][label] = names[int(oid)]

    def query_of(i, up=0):
        while i is not None:
            if nodes[i]["name"] == "QUERY":
                if up == 0:
                    return i
                up -= 1
            i = nodes[i]["parent"]
        return None

    def rtable(q):
        return [k for k, n in enumerate(nodes) if n["parent"] == q and n["field"] == "rtable"]

    def entry_name(k):
        alias = next((n["attrs"].get("aliasname") for n in nodes
                      if n["parent"] == k and n["name"] == "ALIAS" and n["field"] in ("eref", "alias")),
                     None)
        return nodes[k]["attrs"].get("relname") or alias

    columns = {}
    for i, n in enumerate(nodes):
        a = n["attrs"]
        if n["name"] == "RANGETBLENTRY":
            a["table"] = entry_name(i)
            alias = next((c["attrs"].get("aliasname") for c in nodes
                          if c["parent"] == i and c["name"] == "ALIAS" and c["field"] == "alias"), None)
            if alias:
                a["alias"] = alias
        if n["name"] == "CONST":
            value = _const(a)
            if value is not None:
                a["value"] = value
        if n["name"] in ("VAR", "RANGETBLREF"):
            index = a.get("varno") or a.get("rtindex")
            q = query_of(n["parent"], int(a.get("varlevelsup", "0") or 0))
            entries = rtable(q) if q is not None else []
            if index and index.isdigit() and 0 < int(index) <= len(entries):
                entry = entries[int(index) - 1]
                a["table"] = entry_name(entry)
                relid, attno = nodes[entry]["attrs"].get("relid"), a.get("varattno", "")
                if n["name"] == "VAR" and relid and attno.lstrip("-").isdigit():
                    columns.setdefault((int(relid), int(attno)), []).append(a)
    if columns:
        rows = conn.execute(
            "select attrelid, attnum, attname from pg_attribute where (attrelid, attnum) in "
            "(select * from unnest(%s::oid[], %s::int2[]))",
            ([r for r, _ in columns], [c for _, c in columns])).fetchall()
        for relid, attnum, attname in rows:
            for a in columns[(relid, attnum)]:
                a["column"] = attname


def _shown(nodes):
    """The nodes worth listing: an ALIAS node only repeats its table's name."""
    return [{"name": n["name"], "field": n["field"], "depth": n["depth"],
             "attrs": {k: v for k, v in n["attrs"].items() if k != "constvalue"}}
            for n in nodes if n["name"] != "ALIAS"]


def collect(notices, conn=None):
    """notices: [(severity, primary, detail)] of the statement → {query, rewritten, plan}.
    conn, if given, is used to look up the names behind the numbers in the tree."""
    raw = {}
    for _sev, primary, detail in notices:
        kind = TITLES.get((primary or "").strip())
        if kind and detail:
            raw.setdefault(kind, detail)    # the statement's own comes first
    out = {}
    for kind, text in raw.items():
        nodes = _parse(text)
        if conn is not None and kind != "plan":
            _resolve(nodes, conn)
        out[kind] = {"nodes": len(nodes), "bytes": len(text), "text": text}
        if kind != "plan":
            out[kind]["shown"] = _shown(nodes)
    if "query" in out and out.get("rewritten", {}).get("shown") == out["query"]["shown"]:
        out["rewritten"] = {"same_as_query": True, "nodes": out["query"]["nodes"]}
    return out
