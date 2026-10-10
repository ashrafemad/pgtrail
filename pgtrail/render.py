"""Plain-text rendering of a trace. Everything here reads the trace dict only,
so a web UI can replace this module without touching the collectors."""
import sys
import textwrap
from collections import Counter

MAX_PER_RELATION = 6
# the part of the engine each section shows at work (the same names as the talk's pipeline)
PARTS = {
    "WIRE": "wire protocol",
    "STAGES": "parser → analyzer → rewriter → planner → executor",
    "PLAN": "planner, executor and access methods",
    "LOCKS": "lock manager",
    "BUFFER CACHE": "buffer manager",
    "WAL": "write-ahead log",
    "PAGES": "storage: heap and B-tree pages, MVCC row versions",
    "COMMIT": "transaction manager",
    "AFTERMATH": "checkpointer and background writer",
    "SYSCALLS": "operating system",
}
MAX_SYSCALLS_PER_PHASE = 40


class Style:
    def __init__(self, color):
        self.color = color

    def _wrap(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.color else str(text)

    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def head(self, t): return self._wrap("1;36", t)
    def good(self, t): return self._wrap("32", t)
    def warn(self, t): return self._wrap("33", t)
    def bad(self, t): return self._wrap("31", t)


def stage_cost(s):
    """CPU time and page faults of one stage, as words."""
    if s.get("cpu_user_ms") is None:
        return ""
    cpu = (s["cpu_user_ms"] or 0) + (s["cpu_system_ms"] or 0)
    faults = (s.get("minor_page_faults") or 0) + (s.get("major_page_faults") or 0)
    text = f"CPU {cpu:.3f} ms, {faults} page fault{'s' * (faults != 1)}"
    if s.get("major_page_faults"):
        text += f" ({s['major_page_faults']} needed the disk)"
    return text


def request(trace):
    """(label, bytes, words) for what the client sent to start the statement."""
    sent = []
    for m in trace["protocol"]["messages"]:
        if m["dir"] != "client→server":
            break
        sent.append(m)
    size = sum(m["bytes"] for m in sent)
    if any(m["type"] == "Parse" for m in sent):
        return ("PARSE·BIND·EXEC", size,
                f"{len(sent)} messages in one go ({' + '.join(m['type'] for m in sent)}): "
                "the extended protocol, as most drivers send it")
    return "QUERY", size, "one Query message carrying the SQL text: the simple protocol"


TREE_TITLES = {
    "btree": "B-TREE LOOKUP",
    "query": "QUERY TREE",
    "rewritten": "AFTER THE REWRITER",
    "plan": "PLAN TREE",
}
TREE_WORDS = {
    "btree": "how an index scan finds the row: from the root page down to a leaf, then to the table",
    "query": "how the server understood your SQL, from parse analysis; read it top down",
    "rewritten": "the query tree after views and rules were expanded",
    "plan": "the steps the executor runs; read it from the bottom up: each step feeds the one above",
}
_COMMANDS = {"1": "SELECT", "2": "UPDATE", "3": "INSERT", "4": "DELETE", "5": "MERGE"}
_JOINS = {"0": "inner", "1": "left", "2": "full", "3": "right"}
_SUBLINKS = {"0": "an EXISTS (subquery) test", "1": "an op ALL (subquery) test",
             "2": "an IN (subquery) test", "4": "a subquery that returns one value"}
_CASTS = {"numeric", "int2", "int4", "int8", "float4", "float8", "text", "varchar", "bool",
          "date", "timestamp", "timestamptz", "interval"}
# what a node does, by its place in its parent
_FIELD_WORDS = {"quals": "WHERE", "groupClause": "GROUP BY", "havingQual": "HAVING",
                "sortClause": "ORDER BY", "limitCount": "LIMIT", "limitOffset": "OFFSET",
                "returningList": "RETURNING", "cteList": "WITH", "subselect": "the subquery",
                "larg": "left side", "rarg": "right side", "testexpr": "the test"}


def _query_words(n, parent, command):
    a, name = n["attrs"], n["name"]
    if name == "QUERY":
        cmd = _COMMANDS.get(a.get("commandType"), "statement")
        return (f"the {cmd} as the server understands it" if n["depth"] == 0
                else f"a {cmd} inside the statement")
    if name == "RANGETBLENTRY":
        table, kind = a.get("table"), a.get("rtekind", "0")
        if kind == "2":
            return "the result of the join"
        if kind == "1":
            return "the rows a subquery returns"
        alias = f", called {a['alias']}" if a.get("alias") and a["alias"] != table else ""
        return f"a table it uses: {table}{alias}"
    if name == "RTEPERMISSIONINFO":
        return f"check that you may use {a.get('relname', 'the table')}"
    if name == "FROMEXPR":
        return "where the rows come from (FROM) and which to keep (WHERE)"
    if name == "JOINEXPR":
        return f"an {_JOINS.get(a.get('jointype'), '')} join of the two sides below".replace("an  ", "a ")
    if name == "RANGETBLREF":
        return f"rows from {a.get('table', 'a table')}"
    if name == "OPEXPR":
        return f"the {a.get('opname', '?')} operator, on the two values below"
    if name == "SCALARARRAYOPEXPR":
        return f"{a.get('opname', '?')} ANY of a list, as in IN (1, 2, 3)"
    if name == "BOOLEXPR":
        return {"and": "AND: the conditions below must all hold",
                "or": "OR: at least one condition below must hold",
                "not": "NOT: the condition below must not hold"}.get(a.get("boolop"), "a condition")
    if name == "VAR":
        column = a.get("column")
        if column == "ctid":
            return "the row's address (ctid), to find the row again and change it"
        if column:
            return f"column {column}" + (f" of {a['table']}" if a.get("table") else "")
        return "a column"
    if name == "CONST":
        if parent and parent["name"] == "FUNCEXPR" and n.get("position") == 1 and "value" in a:
            # a cast's second argument is the target type's size, packed into one number
            size = int(a["value"]) - 4
            limit = (f"{size >> 16},{size & 0xFFFF}" if parent["attrs"].get("funcname") == "numeric"
                     else str(size))
            return f"the size limit: {parent['attrs'].get('funcname')}({limit})"
        return f"the value {a['value']}" if "value" in a else "a constant value"
    if name == "FUNCEXPR":
        f = a.get("funcname", "?")
        return f"convert the value below to {f}" if f in _CASTS else f"call the function {f}()"
    if name == "AGGREF":
        return f"the {a.get('funcname', '?')}() aggregate"
    if name == "TARGETENTRY":
        col = a.get("resname", "")
        if a.get("resjunk") == "true":
            return f"a helper column {col}, not returned"
        if command == "UPDATE" and n["depth"] == 1:
            return f"set {col} to the value below"
        return f"an output column: {col}"
    if name == "SUBLINK":
        return _SUBLINKS.get(a.get("subLinkType"), "a subquery inside an expression")
    if name == "PARAM":
        return "a value filled in while running (here: from the subquery)"
    if name == "SORTGROUPCLAUSE":
        return "one item of the clause, pointing at an output column"
    return {
        "NULLTEST": "an IS NULL / IS NOT NULL test",
        "CASEEXPR": "a CASE expression", "COALESCEEXPR": "COALESCE(...)",
        "RELABELTYPE": "the same value seen as another type",
        "COERCEVIAIO": "a conversion through text", "COMMONTABLEEXPR": "a WITH query",
        "WINDOWFUNC": "a window function", "ROWEXPR": "a row value (a, b, ...)",
        "MINMAXEXPR": "GREATEST or LEAST", "NULLIFEXPR": "NULLIF(...)",
        "SETTOMULTIPLEEXPR": "a column set from a multi-column value",
    }.get(name, name.lower())


def _plan_words(p):
    t, rel, idx = p["Node Type"], p.get("Relation Name"), p.get("Index Name")
    alias = f" ({p['Alias']})" if p.get("Alias") and p.get("Alias") != rel else ""
    words = {
        "Seq Scan": f"read every row of {rel}{alias}",
        "Index Scan": f"find rows of {rel} through the index {idx}",
        "Index Only Scan": f"answer from the index {idx} alone, without reading {rel}",
        "Bitmap Index Scan": f"mark the matching rows in the index {idx}",
        "Bitmap Heap Scan": f"read the rows of {rel} that were marked below",
        "Hash": "put the rows below into a lookup table in memory",
        "Hash Join": "match each row of the first input below against the lookup table "
                     "built from the second",
        "Nested Loop": "for each row from the first input, look up matches in the second",
        "Merge Join": "walk two sorted inputs side by side, pairing equal keys",
        "Sort": "sort the rows" + (f" by {', '.join(p['Sort Key'])}" if p.get("Sort Key") else ""),
        "Incremental Sort": "finish sorting rows that come partly sorted",
        "Limit": "stop once enough rows have gone out (LIMIT)",
        "Materialize": "keep the rows below in memory, to read them again",
        "Memoize": "remember results for keys already looked up",
        "Result": "compute the result without reading a table",
        "Subquery Scan": f"read the rows the subquery{alias} returns",
        "Unique": "drop duplicate rows",
        "Append": "one input after another",
        "Gather": "collect rows from parallel workers",
        "Gather Merge": "collect sorted rows from parallel workers",
        "Function Scan": "read the rows a function returns",
        "Values Scan": "read the rows of a VALUES list",
        "CTE Scan": "read the rows of a WITH query",
    }.get(t)
    if t == "Aggregate":
        keys = f" by {', '.join(p['Group Key'])}" if p.get("Group Key") else ""
        how = {"Hashed": "in a hash table", "Sorted": "from sorted rows"}.get(p.get("Strategy"), "")
        words = (f"group the rows{keys} {how}, computing the aggregates".replace("  ", " ")
                 if keys else "combine all rows into one result row")
    if t == "ModifyTable":
        op = (p.get("Operation") or "change").lower()
        words = f"{op} the rows it receives in {rel}"
        child = (p.get("Plans") or [{}])[0]
        if "Actual Rows" in child:
            # without RETURNING it passes nothing up: say what it changed instead
            changed = int(child["Actual Rows"] * (child.get("Actual Loops") or 1))
            return f"{words} → {changed:,} row{'s' * (changed != 1)} {op}d".replace("eed", "ed")
    for key, label in (("Index Cond", "where"), ("Hash Cond", "on"), ("Merge Cond", "on"),
                       ("Join Filter", "keeping"), ("Recheck Cond", "rechecking"), ("Filter", "keeping")):
        if p.get(key):
            words = f"{words or t}, {label} {p[key]}"
    if p.get("Join Type") and p["Join Type"] != "Inner" and "Join" in t or t == "Nested Loop" and p.get("Join Type", "Inner") != "Inner":
        words = f"{words} ({p['Join Type'].lower()} join)"
    if "Actual Rows" in p:
        rows = int(p["Actual Rows"] * (p.get("Actual Loops") or 1))
        words = f"{words} → {rows:,} row{'s' * (rows != 1)}"
    extra = [w for w in (estimate_words(p), removed_words(p), memory_words(p)) if w]
    if extra:
        words = f"{words or t} ({'; '.join(extra)})"
    return words or t


def _plan_name(p):
    t = p["Node Type"]
    return {"Aggregate": "AGG", "Nested Loop": "NESTLOOP"}.get(t, t.upper().replace(" ", ""))


def _draw(items):
    """[(depth, name, words)] → [(label with branch lines, continuation prefix, words)]."""
    out, last = [], []
    for i, (depth, name, words) in enumerate(items):
        is_last = True
        for later in items[i + 1:]:
            if later[0] < depth:
                break
            if later[0] == depth:
                is_last = False
                break
        last = last[:depth] + [is_last]
        lines = "".join("  " if done else "│ " for done in last[1:depth])
        branch = ("└ " if is_last else "├ ") if depth else ""
        cont = lines + (("  " if is_last else "│ ") if depth else "")
        out.append((lines + branch + name, cont, words))
    return out


def tree_rows(trace, kind):
    """The tree as rows of (label, continuation prefix, words), or []."""
    if kind == "btree":
        return _draw(btree_lines(trace))
    if kind == "plan":
        plan_nodes(trace)                   # marks what sits below a LIMIT
        plan = trace.get("plan") or {}
        items = []

        def walk(p, depth):
            items.append((depth, _plan_name(p), _plan_words(p)))
            for child in p.get("Plans", []):
                walk(child, depth + 1)
        if plan.get("Plan"):
            walk(plan["Plan"], 0)
        return _draw(items)
    tree = (trace.get("trees") or {}).get(kind) or {}
    if tree.get("same_as_query"):
        return [("unchanged", "", "there were no views or rules to expand")]
    nodes = tree.get("shown", [])
    for i, n in enumerate(nodes):           # position among its parent's children
        n["position"] = sum(1 for m in reversed(nodes[:i]) if m["depth"] == n["depth"]
                            and all(k["depth"] >= n["depth"] for k in nodes[nodes.index(m):i]))
    command = _COMMANDS.get(nodes[0]["attrs"].get("commandType")) if nodes else None
    items = []
    for i, n in enumerate(nodes):
        parent = next((p for p in reversed(nodes[:i]) if p["depth"] == n["depth"] - 1), None)
        words = _query_words(n, parent, command)
        role = _FIELD_WORDS.get(n["field"])
        if n["field"] == "quals" and parent and parent["name"] == "JOINEXPR":
            role = "ON"
        if role:
            words = f"{role}: {words}"
        items.append((n["depth"], n["name"], words))
    return _draw(items)


def tree_pages(trace, kind, width, rows):
    """The tree wrapped to width (a label column, then the words) in pages of rows lines."""
    drawn = tree_rows(trace, kind)
    if not drawn:
        return []
    label_w = min(max(len(label) for label, _, _ in drawn), width // 2)
    lines = []
    for label, cont, words in drawn:
        text = textwrap.wrap(words, max(16, width - label_w - 2)) or [""]
        if len(label) > label_w:
            lines.append(label)
            lines += [cont.ljust(label_w) + "  " + t for t in text]
        else:
            lines.append(label.ljust(label_w) + "  " + text[0])
            lines += [cont.ljust(label_w) + "  " + t for t in text[1:]]
    return [lines[i:i + rows] for i in range(0, len(lines), rows)]


def waiting_stage(trace):
    """The stage that spent its time waiting: elapsed far above its CPU time."""
    gaps = [(s["elapsed_ms"] - (s.get("cpu_user_ms") or 0) - (s.get("cpu_system_ms") or 0), s["stage"])
            for s in trace.get("stages", []) if s.get("elapsed_ms") is not None]
    return max(gaps)[1] if gaps else "execute"


def _what_it_waited_for(seen):
    if not seen or not seen.get("locktype"):
        return "a lock"
    seen = {**seen, "target": seen.get("target", seen.get("relation"))}   # older traces
    if seen["locktype"] == "transactionid":
        return (f"its {seen['mode']} on transaction {seen['target']}: a row that transaction changed. "
                "A row lock waits for the whole other transaction to end")
    if seen["locktype"] == "relation":
        return f"its {seen['mode']} on the table {seen['target']}"
    return f"its {seen['mode']} on a {seen['locktype']}"


def lock_wait(trace):
    """The other-session story as (headline, explanation), or None."""
    w = trace.get("lock_wait")
    if not isinstance(w, dict) or "error" in w:
        return None
    scenario = w.get("scenario", "lock")
    # traces saved before --with only knew the table lock
    w.setdefault("blocked", w.get("waited_ms", 0) > 0)
    w.setdefault("other_pid", w.get("blocker_pid"))
    w.setdefault("other_sql", f"LOCK TABLE {w.get('relation')} IN ACCESS EXCLUSIVE MODE")
    w.setdefault("other_status", "LOCK TABLE")
    seen = w.get("seen_while_waiting") or {}
    stage = waiting_stage(trace)
    cpu = f" Waiting uses no CPU: compare the {stage} stage's time and CPU."
    if scenario == "deadlock":
        d = w["deadlock"]
        first = (trace["meta"].get("deadlock_first") or {}).get("sql", w.get("deadlock_sql"))
        headline = (f"DEADLOCK: broken after {w['waited_ms']:.0f} ms; {d['victim'] or 'nobody'} was cancelled"
                    if d["detected"] else f"no deadlock: waited {w['waited_ms']:.1f} ms")
        words = (f"this session first ran: {first}. The other session (pid {w['other_pid']}) ran: "
                 f"{w['other_sql']}, then: {w.get('deadlock_sql')}, and had to wait for this session. "
                 "Then this statement had to wait for the other session: each waited for the other, a "
                 "cycle that no amount of waiting can end.")
        if d["detected"]:
            words += (f" After deadlock_timeout ({d['timeout']}) PostgreSQL's deadlock detector found the "
                      f"cycle and cancelled {d['victim']}: \"{d['message']}\" ({' '.join(d['detail'].split())})")
        return headline, words + cpu
    if not w.get("blocked"):
        reads = trace["meta"]["sql"].lstrip().lower().startswith(("select", "with"))
        words = (f"another session (pid {w['other_pid']}) ran: {w['other_sql']} ({w['other_status']}) and kept "
                 "its transaction open, but held nothing this statement needed")
        if reads:
            words += (". Readers never wait for writers: MVCC gave this statement the last committed "
                      "version of the rows")
        return "no wait: the other session did not get in the way", words + "."
    if scenario == "lock":
        headline = f"waited {w['waited_ms']:.1f} ms for a lock on {w['relation']}"
        words = (f"another session (pid {w['other_pid']}) held {w['mode']} on {w['relation']}, so the "
                 f"statement queued for {_what_it_waited_for(seen)}")
    else:
        headline = f"waited {w['waited_ms']:.1f} ms for another session"
        words = (f"another session (pid {w['other_pid']}) had run: {w['other_sql']} ({w['other_status']}) and "
                 f"kept its transaction open, so this statement queued for {_what_it_waited_for(seen)}")
    if seen.get("wait_event"):
        words += (f"; halfway, the server reported it waiting on {seen['wait_event_type']}/"
                  f"{seen['wait_event']}, blocked by pid {', '.join(map(str, seen['blocking_pids']))}")
    if w.get("other_end"):
        words += f"; the other session ended with {w['other_end']}"
        if (w["other_end"] == "COMMIT" and seen.get("locktype") == "transactionid"):
            words += ", and this statement re-read the row's new version before changing it"
    return headline, words + "." + cpu


def wait_view(trace):
    """What the games show for the other session: a box, and lines to say."""
    story = lock_wait(trace)
    if not story:
        return None
    w = trace["lock_wait"]
    scenario = w.get("scenario", "lock")
    other = w["other_sql"] if len(w["other_sql"]) <= 52 else w["other_sql"][:51] + "…"
    if scenario == "deadlock" and w["deadlock"]["detected"]:
        art = ["this statement ──waits for──▶ other session",
               "      ▲                                 │",
               "      └────────────waits for────────────┘"]
        width = max(len(line) for line in art)
        box = [line.ljust(width) for line in art] + [
            "", f"after {w['deadlock']['timeout']}, the detector cancels {w['deadlock']['victim']}"]
        return {"kind": "deadlock", "title": "DEADLOCK!", "box": box, "waited": w["waited_ms"],
                "words": story[1], "stage": waiting_stage(trace), "blocked": True}
    if not w.get("blocked"):
        return {"kind": "free", "title": "ANOTHER SESSION, NO WAIT", "box": [f"other session: {other}",
                "held nothing this statement needed"], "waited": 0, "words": story[1],
                "stage": "execute", "blocked": False}
    seen = w.get("seen_while_waiting") or {}
    holder = (f"another session holds {w['mode']} on {w['relation']}" if scenario == "lock"
              else f"other session: {other}")
    lock = (f"wants {seen.get('mode')} on {'transaction ' if seen.get('locktype') == 'transactionid' else ''}"
            f"{seen.get('target', seen.get('relation'))}" if seen.get("mode") else "")
    return {"kind": "blocked", "title": "BLOCKED", "box": [holder] + ([lock] if lock else []),
            "waited": w["waited_ms"], "words": story[1], "stage": waiting_stage(trace), "blocked": True}


def _per_loop_total(node, key):
    return int((node.get(key) or 0) * (node.get("Actual Loops") or 1))


def estimate(node):
    """(estimated rows, actual rows, how many times off) for one plan node, per loop.
    None when the comparison means nothing: below a LIMIT, a node returning fewer
    rows than estimated was just stopped early, as planned."""
    if "Actual Rows" not in node or "Plan Rows" not in node:
        return None
    est, got = node["Plan Rows"], node["Actual Rows"]
    if node.get("_under_limit") and got < est:
        return None
    off = max(est, got) / max(min(est, got), 1)
    return est, got, off


def estimate_words(node):
    e = estimate(node)
    if not e or e[2] < 2:
        return ""
    return f"planner guessed {e[0]:,}, got {e[1]:,}: {e[2]:,.0f}× off"


def removed_words(node):
    gone = _per_loop_total(node, "Rows Removed by Filter") + _per_loop_total(node, "Rows Removed by Join Filter")
    if not gone:
        return ""
    kept = _per_loop_total(node, "Actual Rows")
    share = 100 * gone / (gone + kept) if gone + kept else 0
    return f"read {gone + kept:,} rows, threw away {gone:,} ({share:.0f}%)"


def own_temp(node, key):
    """Temp blocks of this node alone: EXPLAIN counts its children's in it too."""
    return max(0, (node.get(key) or 0) - sum(c.get(key) or 0 for c in node.get("Plans", [])))


def memory_words(node, block_size=8192):
    """Sort, hash and temp-file use of one node, and whether it spilled to disk."""
    parts = []
    if node.get("Sort Method"):
        where = "on disk" if node.get("Sort Space Type") == "Disk" else "in memory"
        parts.append(f"sorted with {node['Sort Method']} {where}, {node.get('Sort Space Used', 0):,} kB")
    if node.get("Hash Batches") is not None:
        spilled = node["Hash Batches"] > 1
        parts.append(f"hash table of {node.get('Hash Buckets', 0):,} buckets, {node.get('Peak Memory Usage', 0):,} kB"
                     + (f", spilled to disk in {node['Hash Batches']} batches" if spilled else ", in memory"))
    elif node.get("HashAgg Batches") is not None:
        spilled = node["HashAgg Batches"] > 1
        parts.append(f"hash table {node.get('Peak Memory Usage', 0):,} kB"
                     + (f", spilled to disk in {node['HashAgg Batches']} batches ({node.get('Disk Usage', 0):,} kB)"
                        if spilled else ", in memory"))
    written, read = own_temp(node, "Temp Written Blocks"), own_temp(node, "Temp Read Blocks")
    if written or read:
        parts.append(f"temp files: {written * block_size // 1024:,} kB written, "
                     f"{read * block_size // 1024:,} kB read")
    if node.get("Heap Fetches") is not None:
        parts.append(f"{node['Heap Fetches']:,} heap fetches (rows the visibility map could not vouch for)")
    return "; ".join(parts)


def spilled(node):
    return (node.get("Sort Space Type") == "Disk" or (node.get("Hash Batches") or 0) > 1
            or (node.get("HashAgg Batches") or 0) > 1 or bool(own_temp(node, "Temp Written Blocks")))


def plan_nodes(trace):
    plan = (trace.get("plan") or {}).get("Plan")
    out = []

    def walk(n, limited):
        n["_under_limit"] = limited
        out.append(n)
        for c in n.get("Plans", []):
            walk(c, limited or n.get("Node Type") == "Limit")
    if plan:
        walk(plan, False)
    return out


def plan_checks(trace):
    """Short sentences: the worst estimate, and any spill to disk."""
    nodes = plan_nodes(trace)
    checks = []
    worst = max((n for n in nodes if estimate(n)), key=lambda n: estimate(n)[2], default=None)
    if worst:
        est, got, off = estimate(worst)
        name = worst.get("Node Type", "?") + (f" on {worst['Relation Name']}" if worst.get("Relation Name") else "")
        if off >= 10:
            checks.append(f"worst estimate: {name}, planner guessed {est:,} rows and got {got:,} "
                          f"({off:,.0f}× off): fresh statistics (ANALYZE) or a better one may change the plan")
        else:
            checks.append(f"estimates held up: the worst was {off:.1f}× off ({name}, guessed {est:,}, got {got:,})")
    spills = [n for n in nodes if spilled(n)]
    work_mem = (trace["meta"].get("settings") or {}).get("work_mem", "work_mem")
    if spills:
        checks.append(f"{len(spills)} step(s) did not fit in work_mem ({work_mem}) and spilled to disk: "
                      + ", ".join(n["Node Type"] for n in spills))
    elif any(memory_words(n) for n in nodes):
        checks.append(f"every sort and hash fit in work_mem ({work_mem})")
    return checks


def stats_lines(trace):
    """What the planner knew about the tables and columns it used, in words."""
    lines = []
    for t in trace.get("statistics") or []:
        if not isinstance(t, dict):
            continue
        lines.append(f"{t['table']}: {t['rows']:,} rows in {t['pages']:,} pages, as of the last ANALYZE")
        if not t["columns"]:
            lines.append("   no column statistics yet: run ANALYZE so the planner has more than guesses")
        for c in t["columns"]:
            what = ("unique: every row has its own value" if c["unique"]
                    else f"{c['distinct']:,} distinct values")
            if c["null_frac"]:
                what += f", {c['null_frac'] * 100:.1f}% NULL"
            if c["most_common"]:
                common = ", ".join(f"{v} ({f * 100:.1f}%)" for v, f in c["most_common"][:3])
                what += f"; most common: {common}"
            if c["histogram"]:
                h = c["histogram"]
                what += f"; histogram {h['min']} … {h['max']} in {h['buckets']} buckets"
            corr = c["correlation"]
            if corr is not None:
                order = ("stored in nearly this order, so range scans read few pages" if abs(corr) > 0.9
                         else "scattered across the table, so range scans jump around" if abs(corr) < 0.3
                         else "partly in this order")
                what += f"; correlation {corr:.2f}: {order}"
            lines.append(f"   {c['column']}: {what}")
    return lines


def no_index_words(trace):
    """Why there is no B-tree walk to show, or "" when there is one (or no plan)."""
    if trace.get("btree"):
        return ""
    scans = [n for n in plan_nodes(trace) if n.get("Node Type") == "Seq Scan" and n.get("Relation Name")]
    if not scans:
        return ""
    tables = " and ".join(dict.fromkeys(n["Relation Name"] for n in scans))
    return (f"No B-tree dive this time: the planner read {tables} in full (Seq Scan), "
            "which it estimated cheaper than any index; the plan comparison shows by how much")


def _cells(children, path, room):
    """Which branches to draw: the first, the last, the one on the path and its
    neighbours, as many as fit; the rest become a gap with its count."""
    keep = {0, path, len(children) - 1}
    near = 1
    while len(keep) < room and len(keep) < len(children):
        for i in (path - near, path + near, near, len(children) - 1 - near):
            if 0 <= i < len(children) and len(keep) < room:
                keep.add(i)
        near += 1
    items, last = [], None
    for i in sorted(keep):
        if last is not None and i - last > 1:
            items.append(("gap", i - last - 1))
        items.append(("cell", i))
        last = i
    return items


def _range(children, i):
    low, high = children[i][1], children[i + 1][1] if i + 1 < len(children) else None
    return f"{'min' if low is None else low} … {'max' if high is None else high - 1}"


def btree_drawing(trace, width=92):
    """Small B-trees drawn page by page, as lines of (text, on the lookup's path?) parts."""
    out = []
    for b in trace.get("btree") or []:
        if not isinstance(b, dict) or not b["path"] or b["height"] > 3:
            continue
        if any(step["level"] > 0 and "children" not in step for step in b["path"]):
            continue                        # a trace from before pages were recorded
        cell = 14
        for depth, step in enumerate(b["path"]):
            if step["level"] == 0:
                break
            children = step["children"]
            path = next(i for i, c in enumerate(children) if c[0] == step["child"])
            room = max(3, (width - 16) // cell)
            items = _cells(children, path, room)
            widths = [cell if kind == "cell" else len(f" {n} more ") + 4 for kind, n in items]
            total = sum(widths)
            centers, x = [], 0
            for w in widths:
                centers.append(x + w // 2)
                x += w
            mid = total // 2
            target = next(c for (kind, n), c in zip(items, centers) if kind == "cell" and n == path)
            first, last = centers[0], centers[-1]
            line, hl = [" "] * total, [False] * total
            for i in range(min(first, mid), max(last, mid) + 1):
                line[i] = "─"
            x = 0
            for (kind, n), w, c in zip(items, widths, centers):
                if kind == "cell":
                    line[c] = "┬"
                else:
                    label = f" {n} more "
                    start = x + (w - len(label)) // 2
                    line[start:start + len(label)] = list(label)
                x += w
            line[min(first, mid)], line[max(last, mid)] = "┌", "┐"
            line[mid] = "┼" if line[mid] == "┬" else "┴"
            if mid == min(first, mid) or mid == max(last, mid):
                line[mid] = "├" if mid == min(first, mid) else "┤"
            # color the way from the parent to the branch taken
            for i in range(min(mid, target), max(mid, target) + 1):
                hl[i] = line[i] not in " " and not line[i].isalnum()
            parent = f"{step['kind'].upper()} · block {step['block']} · {step['items']:,} keys"
            pad = max(0, mid - len(parent) // 2)
            out.append([(" " * pad, False), (parent, True)])
            out.append([(" " * mid, False), ("│", True)])
            out.append(_segments(line, hl))
            for row in range(3):
                text, mask = [" "] * max(total, 1), [False] * max(total, 1)
                x = 0
                for (kind, n), w in zip(items, widths):
                    if kind == "cell" and row < 2:
                        child_kind = "LEAF" if step["level"] == 1 else "INNER"
                        words = [f"{child_kind} {children[n][0]}", _range(children, n)][row][: w - 1]
                        start = x + (w - len(words)) // 2
                        text[start:start + len(words)] = list(words)
                        if n == path:
                            for i in range(start, start + len(words)):
                                mask[i] = True
                    x += w
                if row == 2:                # under the branch taken: where it leads
                    nxt = b["path"][depth + 1] if depth + 1 < len(b["path"]) else None
                    words = (f"→ row {nxt['found']}" if nxt and nxt["level"] == 0 and nxt.get("found")
                             else "↓" if nxt and nxt["level"] > 0 else "")
                    if not words:
                        continue
                    start = max(0, min(target - len(words) // 2, total - len(words)))
                    text = [" "] * max(total, start + len(words))
                    mask = [False] * len(text)
                    text[start:start + len(words)] = list(words)
                    mask[start:start + len(words)] = [True] * len(words)
                out.append(_segments(text, mask))
            out.append([("", False)])
        out.append([(f"colored: the way down to {b['column']} = {b['lookup']}, and the leaf it lives in",
                     True)])
    return out


def _segments(chars, mask):
    parts, current, flag = [], "", None
    for ch, m in zip(chars, mask):
        if flag is not None and m != flag:
            parts.append((current, flag))
            current = ""
        current += ch
        flag = m
    if current:
        parts.append((current.rstrip() if not flag else current, flag))
    return parts


def btree_pages(trace, width, rows):
    """The drawing, then the walk in words, cut into pages of rows lines."""
    lines = btree_drawing(trace, width)
    for text in tree_pages(trace, "btree", width, 10_000)[0] if tree_rows(trace, "btree") else []:
        lines.append([(text, False)])
    # one width for all, so a box that centers each line keeps the columns in line
    full = max((sum(len(t) for t, _ in parts) for parts in lines), default=0)
    lines = [parts + [(" " * (full - sum(len(t) for t, _ in parts)), False)] for parts in lines]
    return [lines[i:i + rows] for i in range(0, len(lines), rows)]


def btree_lines(trace):
    """Each index the plan used; for a lookup, its descent from root to row."""
    rows = []
    for b in trace.get("btree") or []:
        if not isinstance(b, dict):
            continue
        rows.append((0, b["index"].upper(), f"a B-tree on {b['table']}({b['column']}): {b['height']} "
                     f"level{'s' * (b['height'] != 1)}, {b['pages']:,} pages, root block {b['root']}"))
        for step in b["path"]:
            if step["level"] > 0:
                where = (f"{b['lookup']} is below every other key on this page, so follow the first branch"
                         if step.get("key") is None else
                         f"{b['lookup']} comes after key {step['key']}, so follow that branch")
                rows.append((1, f"{step['kind'].upper()} {step['block']}",
                             f"{step['items']:,} keys; {where}, down to block {step['child']}"))
            else:
                entries = step.get("entries", [])
                old = sum(1 for e in entries if not e["current"])
                noun = "row" if step.get("posting_lists") else "index entr"
                plural = ("s" if noun == "row" else "ies") if len(entries) != 1 else ("" if noun == "row" else "y")
                words = f"{step['items']:,} keys; {len(entries)} {noun}{plural} for {b['lookup']}"
                if step.get("posting_lists"):
                    words += (f", {step['posting_lists']} of them packed into one entry each "
                              "with a list of rows (deduplication)" if step["posting_lists"] > 1 else
                              ", packed into one entry with a list of rows (deduplication)")
                if old:
                    words += (f": {old} point{'s' * (old == 1)} at {'an old row version' if old == 1 else 'old row versions'},"
                              " kept until VACUUM")
                hit = next((e for e in entries if e["current"]), None)
                live = [e for e in entries if e["current"]]
                if hit and hit.get("hot_chain"):
                    words += (f"; the live one goes through a {len(hit['hot_chain'])}-step HOT chain "
                              f"to row {step['found']}")
                elif len(live) > 1:
                    words += f"; the live rows are at {', '.join(e['row'] for e in live[:5])}"
                elif hit:
                    words += f"; the live one points at row {step['found']}"
                rows.append((1, f"LEAF {step['block']}", words))
    return rows


def considered(trace):
    """The plans the planner compared, chosen first, then by estimated cost."""
    plans = trace.get("plans")
    return plans.get("considered", []) if isinstance(plans, dict) else []


def plan_name(p, chosen=None, instead=True):
    """What sets a plan apart: the steps it has that the chosen plan lacks."""
    if chosen is None or p is chosen:
        return f"{p['method']} {p['target']}".strip()
    mine, theirs = Counter(p["summary"].split(" + ")), Counter(chosen["summary"].split(" + "))
    extra, gone = list((mine - theirs).elements()), list((theirs - mine).elements())
    if not extra:
        return p["summary"]
    name = " + ".join(extra)
    if instead and gone and len(extra) + len(gone) <= 3:
        name += " instead of " + " + ".join(gone)
    return name


def plan_table(plans, width, upper=False):
    """(text, plan index, first row?) for a box: names wrapped to width, cost on the first row."""
    chosen, rows = plans[0], []
    for i, p in enumerate(plans):
        name = plan_name(p, None if p["chosen"] else chosen, instead=False)
        for j, part in enumerate(textwrap.wrap(name.upper() if upper else name, width) or [""]):
            cost = f"{p['total_cost']:>10,.2f}" if j == 0 else ""
            rows.append((f"{part:<{width}} {cost:>10}", i, j == 0))
    return rows


def ratio_text(r):
    return "same cost" if r < 1.005 else f"{r:.1f}× the cost" if r < 100 else f"{r:,.0f}× the cost"


def why_chosen(trace):
    """One sentence on why the chosen plan won, from the estimates."""
    plans = considered(trace)
    if len(plans) < 2:
        return (trace.get("plans") or {}).get("note") or "no alternatives to compare"
    chosen, runner, worst = plans[0], plans[1], plans[-1]
    if runner["ratio"] < 1.01:
        if chosen["startup_cost"] < runner["startup_cost"]:
            why = (f"a near tie with {plan_name(runner, chosen)}; the chosen plan can start "
                   f"returning rows sooner (startup {chosen['startup_cost']:.2f} vs "
                   f"{runner['startup_cost']:.2f})")
        else:
            why = f"a near tie with {plan_name(runner, chosen)}"
    else:
        why = f"the next best, {plan_name(runner, chosen)}, was estimated at {runner['ratio']:.2f}× the cost"
    if worst is not runner:
        why += f". {plan_name(worst, chosen)} was {worst['ratio']:.1f}×"
    return why + "."


def _ms(v):
    return "     n/a" if v is None else f"{v:8.3f}"


def _unavailable(out, st, section):
    if isinstance(section, dict) and "error" in section:
        out.append("   " + st.warn("unavailable: " + section["error"]))
        return True
    return False


def _messages(out, st, messages):
    for m in messages:
        arrow = "→" if m["dir"].startswith("client") else "←"
        name = m["type"] + (f" ×{m['count']}" if m["count"] > 1 else "")
        out.append(f"   +{m['at_ms']:7.3f} ms  {arrow} {name:<22} {m['bytes']:>6} B  {st.dim(m['label'])}")


def _plan_node(out, st, node, prefix="", last=True, root=True):
    # (plan_nodes has marked which nodes sit below a LIMIT)
    title = node.get("Node Type", "?")
    if node.get("Operation"):
        title = node["Operation"]
    if node.get("Index Name"):
        title += f" using {node['Index Name']}"
    if node.get("Relation Name"):
        title += f" on {node['Relation Name']}"
    branch = "" if root else ("└─ " if last else "├─ ")
    facts = (f"rows={node.get('Actual Rows', '?')} loops={node.get('Actual Loops', '?')} "
             f"time={node.get('Actual Total Time', '?')} ms")
    out.append(f"   {prefix}{branch}{st.bold(title)}  {facts}")
    pad = prefix + ("" if root else ("   " if last else "│  ")) + "   "
    detail = []
    buf = [f"{label} {node[key]}" for key, label in (
        ("Shared Hit Blocks", "hit"), ("Shared Read Blocks", "read"),
        ("Shared Dirtied Blocks", "dirtied"), ("Shared Written Blocks", "written"))
        if node.get(key)]
    if buf:
        detail.append("buffers: " + ", ".join(buf))
    if node.get("I/O Read Time"):
        detail.append(f"read I/O {node['I/O Read Time']} ms")
    if node.get("WAL Records"):
        detail.append(f"WAL: {node['WAL Records']} records, {node['WAL Bytes']} B"
                      + (f", {node['WAL FPI']} full-page images" if node.get("WAL FPI") else ""))
    for key in ("Index Cond", "Filter", "Hash Cond", "Join Filter"):
        if node.get(key):
            detail.append(f"{key}: {node[key]}")
    for words in (estimate_words(node), removed_words(node), memory_words(node)):
        if words:
            detail.append(words)
    for d in detail:
        out.append(f"   {pad}{st.dim(d)}")
    kids = node.get("Plans", [])
    child_prefix = prefix + ("" if root else ("   " if last else "│  "))
    for i, kid in enumerate(kids):
        _plan_node(out, st, kid, child_prefix, i == len(kids) - 1, False)


DETAILS = ("summary", "detailed", "full")


def render(trace, color=None, detail="full"):
    """The text report. summary: one screen; detailed: every section but the
    server's trees, page bytes, B-tree drawings and syscalls; full: everything."""
    st = Style(sys.stdout.isatty() if color is None else color)
    out, n = [], 0
    full = detail == "full"

    def section(title, note=""):
        nonlocal n
        n += 1
        out.append("")
        part = PARTS.get(title, PARTS["COMMIT"] if title == "ROLLBACK" else "")
        out.append(st.head(f"{n:>2}  {title}") + (st.warn(f"  [{part}]") if part else "")
                   + (st.dim("  " + note) if note else ""))

    meta, result = trace["meta"], trace["result"]
    out.append(st.bold("pgtrail") + st.dim(
        f"  PostgreSQL {meta['server_version']} · backend pid {meta['backend_pid']} · "
        f"xid {meta.get('xid') or 'none (read-only so far)'} · "
        f"{'cold' if meta['cold_backend'] else 'warm'} backend"))
    out.append("")
    out.append("    " + st.bold(meta["sql"]))
    if detail == "summary":
        return "\n".join(out + _summary(trace, st))

    # 1 ------------------------------------------------------------------
    section("WIRE", "what crossed the connection, timed from the first byte sent")
    _messages(out, st, trace["protocol"]["messages"])
    if not trace["protocol"]["messages"]:
        out.append("   " + st.warn("protocol trace unavailable: psycopg exposes libpq tracing on Linux only"))
    out.append(f"   round trip {trace['protocol']['round_trip_ms']:.3f} ms as seen by the client")
    if result["error"]:
        out.append("   " + st.bad("statement failed: " + result["error"]))
    elif result["columns"]:
        out.append(f"   {result['row_count']} row(s): " + st.dim(
            " | ".join(result["columns"]) + "  " +
            "  ".join("(" + ", ".join(r) + ")" for r in result["sample"])))

    # 2 ------------------------------------------------------------------
    section("STAGES", "time inside the backend, reported by the server itself")
    if not trace["stages"]:
        out.append("   " + st.dim("no stage statistics reported"))
    for s in trace["stages"]:
        extra = ""
        if s["device_blocks_in"]:
            extra = st.warn(f"  {s['device_blocks_in'] * 512} B read from the device, not the page cache")
        cost = stage_cost(s)
        out.append(f"   {s['stage']:<8}{_ms(s['elapsed_ms'])} ms  {cost:<34}{st.dim(s['what'])}{extra}")
    if trace["stages"]:
        out.append("   " + st.dim("CPU = time on the processor, user + kernel; a page fault is a memory page "
                                  "the OS had to map in for the backend"))

    trees = (trace.get("trees") if isinstance(trace.get("trees"), dict) else {}) if full else {}
    if "error" in trees:
        _unavailable(out, st, trees)
    for kind in ("query", "rewritten", "plan") if full else ():
        tree = trees.get(kind) or {}
        pages = tree_pages(trace, kind, 92, 10_000)
        if not pages:
            continue
        size = f"{tree['nodes']} nodes in the server's tree" if tree.get("nodes") and not tree.get("same_as_query") else ""
        out.append("")
        out.append(f"   {st.bold(TREE_TITLES[kind])}" + (f"  {st.dim(size)}" if size else ""))
        out.append("   " + st.dim(TREE_WORDS[kind]))
        for line in pages[0]:
            out.append("      " + line)
    if trees:
        out.append("   " + st.dim("every node and field of the server's trees is in --json"))

    # 3 ------------------------------------------------------------------
    section("PLAN", "as executed; a node's buffer counts include its children")
    if trace["plan"]:
        plan_nodes(trace)
        _plan_node(out, st, trace["plan"]["Plan"])
        for check in plan_checks(trace):
            for i, line in enumerate(textwrap.wrap(check, 96)):
                out.append("   " + ("" if i == 0 else "  ") + (st.warn(line) if "spilled" in check
                                                               or "worst estimate" in check else st.dim(line)))
    else:
        out.append("   " + st.dim("no plan: the statement failed before it was planned" if result["error"]
                                  else "no plan: utility statements such as DDL are not planned"))
    plans = considered(trace)
    if plans:
        out.append("")
        out.append(f"   {st.bold('PLANS CONSIDERED')}  "
                   + st.dim("the planner's estimates; only the chosen plan ran"))
        for i, p in enumerate(plans, 1):
            mark = st.good("✔") if p["chosen"] else " "
            ratio = "" if p["chosen"] else f"{p['ratio']:.2f}×"
            head = f"   {mark} {i}  cost {p['total_cost']:>10,.2f} {ratio:>8}   "
            indent = len(f"   x {i}  cost {p['total_cost']:>10,.2f} {ratio:>8}   ")
            for j, part in enumerate(textwrap.wrap(p["summary"], max(30, 100 - indent))):
                line = (head if j == 0 else " " * indent) + part
                out.append(st.bold(line) if p["chosen"] else line)
        for i, line in enumerate(textwrap.wrap("why: " + why_chosen(trace), 96)):
            out.append("   " + ("" if i == 0 else "     ") + st.dim(line))
        if len(plans) > 1:
            off = "; ".join(f"SET enable_{s.removeprefix('enable_')} = off" for s in plans[-1]["disabled"])
            out.append("   " + st.dim(f"to see the last one yourself: BEGIN; {off}; EXPLAIN <statement>; ROLLBACK;"))
        if len(plans) > 1:
            out.append("   " + st.dim("costs are in the planner's units: about 1 per page read in sequence"))
    note = (trace.get("plans") or {}).get("note") if isinstance(trace.get("plans"), dict) else None
    if note and len(plans) > 1:
        out.append("   " + st.dim(note))
    stats = stats_lines(trace)
    if stats:
        out.append("")
        out.append(f"   {st.bold('STATISTICS THE PLANNER USED')}  "
                   + st.dim("pg_stats and pg_class: the estimates above come from these"))
        for line in stats:
            indent = len(line) - len(line.lstrip())
            for i, part in enumerate(textwrap.wrap(line.strip(), 94 - indent)):
                out.append("   " + " " * indent + ("" if i == 0 else "    ") + part)
    drawn = tree_rows(trace, "btree")
    if no_index_words(trace):
        out.append("")
        out.append(f"   {st.bold('B-TREE INDEXES')}  " + st.dim(no_index_words(trace)))
    if drawn:
        out.append("")
        out.append(f"   {st.bold('B-TREE INDEXES')}  " + st.dim("read back with pageinspect, root to leaf"))
        for parts in btree_drawing(trace, 92) if full else []:
            out.append("      " + "".join(st.good(t) if on else t for t, on in parts).rstrip())
        for line in tree_pages(trace, "btree", 92, 10_000)[0]:
            out.append("      " + line)

    # 4 ------------------------------------------------------------------
    section("LOCKS", "held by the backend once the statement finished")
    waited = lock_wait(trace)
    if waited:
        out.append("   " + st.warn(st.bold(waited[0].upper())))
        for line in textwrap.wrap(waited[1], 96):
            out.append("   " + st.dim(line))
        out.append("")
    elif isinstance(trace.get("lock_wait"), dict):
        out.append("   " + st.warn(trace["lock_wait"]["error"]))
    if not _unavailable(out, st, trace["locks"]):
        for l in trace["locks"]:
            tags = ("fast path" if l["fastpath"] else "shared lock table") + ("" if l["granted"] else ", WAITING")
            out.append(f"   {l['mode']:<20} {l['target']:<26} {st.dim(tags + ' · ' + l['why'])}")
        if not trace["locks"]:
            out.append("   " + st.dim("none"))

    # 5 ------------------------------------------------------------------
    section("BUFFER CACHE", "pages in shared_buffers whose state changed")
    b = trace["buffers"]
    if not _unavailable(out, st, b):
        hit_only = []
        for g in b["relations"]:
            kind = ("catalog " if g["system"] else "") + g["kind"]
            name = g["name"] + ("" if g["fork"] == "main" else f" [{g['fork']}]")
            if g["system"] and not any(p["loaded"] or p["modified"] or p["dirtied"] for p in g["pages"]):
                hit_only.append(f"{name} ({len(g['pages'])})")
                continue
            out.append(f"   {st.bold(name)} {st.dim('(' + kind + ')')}")
            if g.get("file"):
                out.append("      " + st.dim("file " + g["file"]))
            for p in g["pages"][:MAX_PER_RELATION]:
                what = []
                if p["loaded"]:
                    what.append(("read in", st.warn))
                if p["modified"]:
                    what.append(("modified", st.bad))
                elif p["dirtied"]:
                    what.append(("dirtied, no WAL record", st.warn))
                if not what:
                    what.append(("hit", st.good))
                plain = " + ".join(w for w, _ in what)
                usage = f"usage {'-' if p['usage_before'] is None else p['usage_before']}→{p['usage_after']}"
                out.append(f"      block {p['block']:<7} " + " + ".join(f(w) for w, f in what)
                           + " " * max(1, 26 - len(plain)) + st.dim(usage))
            more = len(g["pages"]) - MAX_PER_RELATION
            if more > 0:
                out.append("      " + st.dim(f"… and {more} more block(s)"))
        if hit_only:
            out.append("   " + st.dim("catalog pages only read (blocks): " + ", ".join(hit_only)))
        s = b["summary"]
        parts = [text for count, text in (
            (s["loaded"], f"{s['loaded']} read in from the OS"),
            (s["modified"], f"{s['modified']} modified (WAL-logged)"),
            (s["dirtied_without_wal"], f"{s['dirtied_without_wal']} dirtied without a WAL record of its own "
                                       "(hint bits, free space or visibility map)"),
            (s["evicted"], f"{s['evicted']} evicted to make room"),
        ) if count]
        out.append(f"   {s['touched']} page(s) changed state" + (": " + ", ".join(parts) if parts else ""))
        out.append("   " + st.dim("usage count stops at 5, so hits on very hot pages are invisible here; "
                                  "the plan's 'hit' numbers are exact"))

    # 6 ------------------------------------------------------------------
    section("WAL", "records written to the log, in order")
    w = trace["wal"]
    if not _unavailable(out, st, w):
        for r in w["records"]:
            blocks = ", ".join(
                f"{x['relation']}{'' if x['fork'] == 'main' else '[' + x['fork'] + ']'} blk {x['block']}"
                + (" FPI" if x["full_page_image"] else "") for x in r["blocks"])
            kind = f"{r['resource_manager']}/{r['record_type']}"
            size = f"{r['bytes']} B" + (f" ({r['fpi_bytes']} FPI)" if r["fpi_bytes"] else "")
            line = f"   {r['lsn']:<12} {kind:<26} {size:>20}  {blocks}"
            if r["owner"] != "statement":
                cleanup = r["resource_manager"] in ("Heap", "Heap2", "Btree")
                line = st.dim(line + ("  (no xid: page cleanup by this statement, or background work)"
                                      if cleanup else "  (no xid: background)"))
            out.append(line)
        if not w["records"]:
            out.append("   " + st.dim("nothing written; reads do not touch the log"))
        else:
            out.append(f"   {w['statement_records']} record(s), {w['statement_bytes']} bytes for this transaction")
            for f in w.get("files", []):
                out.append("   " + st.dim("file " + f))
        if any(r["fpi_bytes"] for r in w["records"]):
            out.append("   " + st.dim("FPI = full-page image: the first change to a page after a checkpoint "
                                      "logs the whole page"))
        if w["other_transactions_hidden"]:
            out.append("   " + st.dim(f"{w['other_transactions_hidden']} record(s) from other transactions hidden"))

    # 7 ------------------------------------------------------------------
    if "pages" in trace and full:
        section("PAGES", "the blocks named by those WAL records, read back with pageinspect")
        pg = trace["pages"]
        if not _unavailable(out, st, pg):
            for p in pg["pages"]:
                h = p["header"]
                if p["type"] == "heap":
                    states = ", ".join(f"{v} {k}" for k, v in p["line_pointer_states"].items())
                    out.append(f"   {st.bold(p['relation'])} block {p['block']} (heap)  "
                               f"{h['line_pointers']} line pointers ({states}), "
                               f"lower {h['lower']}, upper {h['upper']}, {h['free_bytes']} B free, page LSN {h['lsn']}")
                    for c in p["changes"]:
                        where = f"     lp {c['lp']:<4} at offset {c['offset']:<5} {c['length']:>4} B  "
                        if c["action"] == "created":
                            row = f" {c['row']}" if c.get("row") else ""
                            hot = " (heap-only tuple: no new index entry)" if c["heap_only"] else ""
                            out.append(where + st.good(f"new tuple, xmin {c['xmin']}") + row + st.dim(hot))
                        elif c["action"] == "superseded":
                            hot = " (HOT chain)" if c["hot"] else ""
                            out.append(where + st.bad(f"old version, xmax {c['xmax']}")
                                       + f" → new version at {c['new_version']}" + st.dim(hot))
                        elif c["action"] == "deleted":
                            out.append(where + st.bad(f"deleted, xmax {c['xmax']}")
                                       + st.dim(" (bytes stay until VACUUM)"))
                        else:
                            out.append(where + st.warn(f"row lock, xmax {c['xmax']}"))
                elif p["node"] == "metapage":
                    out.append(f"   {st.bold(p['relation'])} block 0 (btree metapage)  "
                               f"root at block {p['root_block']}, height {p['tree_height']}")
                else:
                    out.append(f"   {st.bold(p['relation'])} block {p['block']} (btree {p['node']}, level {p['level']})  "
                               f"{p['live_items']} live items, {p['dead_items']} dead, {p['free_bytes']} B free, "
                               f"page LSN {h['lsn']}")
                    for e in p["entries"]:
                        out.append(f"     item {e['item']:<4} " + st.good(f"→ heap tuple {e['points_to']}")
                                   + st.dim(f"  key bytes {e['key_bytes']}"))
            if pg["not_shown"]:
                out.append("   " + st.dim(f"{pg['not_shown']} more page(s) not shown (--max-pages)"))
            if not pg["pages"] and not pg["not_shown"]:
                out.append("   " + st.dim("no pages were modified"))

    # 8 ------------------------------------------------------------------
    end = trace["end"]
    section(end["command"])
    _messages(out, st, [m for m in end["messages"] if m["type"] != "NoticeResponse"])
    sync = meta["settings"].get("synchronous_commit", "on")
    if end["command"] == "COMMIT" and meta.get("xid"):
        out.append(f"   {end['round_trip_ms']:.3f} ms. With synchronous_commit = {sync}, the backend "
                   + ("waits here until the WAL is flushed to disk." if sync != "off" else "does not wait for the flush."))
        out.append("   " + st.dim(f"transaction {meta['xid']} is now marked committed in pg_xact, the commit "
                                  "log; from here on, other sessions see its row versions (MVCC)"))
    elif end["command"] == "COMMIT":
        out.append(f"   {end['round_trip_ms']:.3f} ms. No xid was assigned, so there is nothing to flush.")
        out.append("   " + st.dim("a read-only statement needs no transaction id: there is nothing to record "
                                  "in pg_xact"))
    else:
        out.append(f"   {end['round_trip_ms']:.3f} ms. A rollback needs no flush; the changed pages stay dirty anyway.")
        if meta.get("xid"):
            out.append("   " + st.dim(f"transaction {meta['xid']} is marked aborted in pg_xact: its row versions "
                                      "stay on the pages, but no session will ever see them (MVCC)"))

    # 9 ------------------------------------------------------------------
    if "aftermath" in trace:
        a = trace["aftermath"]
        section("AFTERMATH", "what is still only in memory")
        if a["dirtied"] == 0:
            out.append("   " + st.dim("no pages were changed"))
        else:
            out.append(f"   {a['still_dirty']} of {a['dirtied']} changed page(s) are still dirty in shared_buffers. "
                       "The data files have not been written;")
            out.append("   only the WAL has the change. The checkpointer or background writer writes the pages later.")
        if "checkpoint_ms" in a:
            out.append(f"   CHECKPOINT forced: {a['checkpoint_ms']:.1f} ms, "
                       f"{a['dirty_after_checkpoint']} page(s) still dirty afterwards.")
        elif a["dirtied"]:
            out.append("   " + st.dim("run again with --checkpoint to force that write and watch it"))

    # 10 -----------------------------------------------------------------
    if "syscalls" in trace and full:
        section("SYSCALLS", "strace on the backend and the background processes")
        sc = trace["syscalls"]
        if not _unavailable(out, st, sc):
            phases = []
            for e in sc["events"]:
                if e["phase"] not in phases:
                    phases.append(e["phase"])
            for ph in phases:
                evs = [e for e in sc["events"] if e["phase"] == ph]
                # Outside the statement and its commit, a checkpoint writes every
                # dirty page in the instance; show only what belongs to this statement.
                focus = ph in ("statement", "commit", "rollback")
                shown = [e for e in evs if focus or e["statement_page"]]
                out.append("   " + st.bold(ph))
                for e in shown[:MAX_SYSCALLS_PER_PHASE]:
                    took = "" if e["took_ms"] is None else f"{e['took_ms']:.3f} ms"
                    mark = st.bad("  ← changed by this statement") if e["statement_page"] and not focus else ""
                    out.append(f"   +{e['at_ms']:9.3f} ms  {e['process']:<17} {e['syscall']:<10} "
                               f"{e['target']:<30} {e['meaning']}  {st.dim(took)}{mark}")
                rest = len(evs) - min(len(shown), MAX_SYSCALLS_PER_PHASE)
                if rest > 0:
                    out.append("   " + st.dim(f"… and {rest} other syscall(s): other dirty pages, housekeeping "
                                              "(--json has all of them)"))
            if meta.get("tls"):
                out.append("   " + st.dim(f"the connection uses {meta['tls']}, so socket sizes include the TLS record "
                                          "overhead; add sslmode=disable to the DSN for raw sizes"))
            out.append("   " + st.dim("strace slows the traced processes, so timings above are inflated in this run"))
    if detail == "detailed":
        out += ["", "   " + st.dim("detailed view: --detail full adds the server's trees, the page bytes, "
                                    "the B-tree drawings and syscalls")]
    return "\n".join(out)


def _summary(trace, st):
    """One screen: how long, the stages, the plan in words, the warnings, what it touched."""
    from .actual import _steps, touched_files     # actual.py imports this module
    meta, result, proto, end = trace["meta"], trace["result"], trace["protocol"], trace["end"]
    rt, commit = proto["round_trip_ms"], end["round_trip_ms"]
    out = ["", st.head(" SUMMARY")]
    status = st.bad("error: " + result["error"].splitlines()[0]) if result["error"] else (result["status"] or "")
    out.append(f"   {st.good(st.bold(f'{rt + commit:.3f} ms'))}  "
               + st.dim(f"statement {rt:.3f} ms + {end['command']} {commit:.3f} ms") + f"   {status}")
    stages = " · ".join(f"{s['stage']} {s['elapsed_ms']:.3f}" for s in trace.get("stages", [])
                        if s.get("elapsed_ms") is not None)
    if stages:
        out.append("   " + st.dim("stages: ") + stages + " ms")
    waited = lock_wait(trace)
    if waited:
        out.append("   " + st.warn(waited[0]))
    count, parts = _steps(trace)
    for i, line in enumerate(textwrap.wrap(f"{count:,} steps: " + ", ".join(parts), 94)):
        out.append("   " + ("" if i == 0 else "  ") + line)
    pages = tree_pages(trace, "plan", 92, 10_000)
    if pages:
        out += ["", st.head(" PLAN") + st.dim("  read from the bottom up; each step feeds the one above")]
        out += ["   " + line for line in pages[0]]
    plans = considered(trace)
    if len(plans) > 1:
        runner = plans[1]
        out.append("   " + st.dim(f"chosen over {len(plans) - 1} other plan(s); the next best, "
                                  f"{plan_name(runner)}, was estimated at {runner['ratio']:.2f}× the cost"))
    for check in plan_checks(trace):
        for i, line in enumerate(textwrap.wrap(check, 94)):
            out.append("   " + ("" if i == 0 else "  ") + (st.warn(line) if "spilled" in check
                                                         or "worst estimate" in check else st.dim(line)))
    files = touched_files(trace)
    if files:
        out += ["", st.head(" FILES") + st.dim("  on the database server")]
        out += [f"   {path}  " + st.dim(what) for path, what in files]
    out += ["", "   " + st.dim("summary view: --detail detailed or full for every layer")]
    return out
