"""--pace actual: no animation, just how long it really took.

The slow paces walk through every step one at a time; this is the punchline
after them. The statement takes about a millisecond, so the answer is printed
at once: the measured time of each step and how many steps fit in it.
"""
import sys
import textwrap

from .arcade import STAGE_ORDER
from .render import Style, btree_lines, considered, lock_wait, plan_checks, plan_name, stage_cost

FRAME_MS = 1000 / 60                    # one refresh of a 60 Hz screen
BLINK_MS = 100                          # a quick blink of an eye, roughly
BAR = 30                                # cells for the slowest step


def _steps(t):
    """What the slow paces show one by one, counted."""
    proto, end = t["protocol"], t["end"]
    wal = t["wal"].get("records", []) if isinstance(t["wal"], dict) else []
    buffers = t["buffers"].get("summary", {}) if isinstance(t["buffers"], dict) else {}
    locks = [lk for lk in t["locks"] if lk["locktype"] == "relation"] if isinstance(t["locks"], list) else []
    fsync = int(end["command"] == "COMMIT" and bool(wal))
    # NoticeResponse carries pgtrail's own instrumentation; a normal client gets none
    messages = sum(m["count"] for m in proto["messages"] + end["messages"]
                   if m["type"] != "NoticeResponse")
    # the plan's buffer counts are exact; the buffer diff misses hits on hot pages
    root = t["plan"].get("Plan", {}) if isinstance(t.get("plan"), dict) else {}
    if "Shared Hit Blocks" in root:
        read = root.get("Shared Read Blocks", 0)
        pages = root["Shared Hit Blocks"] + read
    else:
        pages, read = buffers.get("touched", 0), buffers.get("loaded", 0)
    parts = [
        (messages, "protocol message", ""),
        (len(t.get("stages", [])), "stage", ""),
        (len(locks), "lock", ""),
        (pages, "page access", f" ({read} read from the OS)"),
        (len(wal), "WAL record", f" ({sum(r['bytes'] for r in wal):,} B)"),
        (fsync, "fsync", ""),
    ]
    return sum(n for n, *_ in parts), [
        f"{n} {what}{('es' if what.endswith('s') else 's') * (n != 1 and what != 'fsync')}{extra}"
        for n, what, extra in parts if n]


def touched_files(t):
    """(path, what) for each data file the statement's pages live in, then the WAL."""
    files = {}
    groups = t["buffers"].get("relations", []) if isinstance(t["buffers"], dict) else []
    for g in groups:
        for p in g["pages"]:
            path = p.get("file") or g.get("file")
            if path:
                fork = "" if g["fork"] == "main" else f" [{g['fork']}]"
                files.setdefault(path, f"{g['name']}{fork}")
    wal = t["wal"].get("files", []) if isinstance(t["wal"], dict) else []
    return list(files.items()) + [(f, "WAL segment") for f in wal]


def render_actual(t, color=None):
    st = Style(sys.stdout.isatty() if color is None else color)
    proto, end = t["protocol"], t["end"]
    rt, commit = proto["round_trip_ms"], end["round_trip_ms"]
    total = rt + commit
    stages = [(s["stage"], s.get("elapsed_ms") or 0) for s in t.get("stages", [])]
    stages.sort(key=lambda s: STAGE_ORDER.index(s[0]) if s[0] in STAGE_ORDER else 99)
    other = max(rt - sum(ms for _, ms in stages), 0)
    rows = stages + [("wire+other", other), (end["command"], commit)]
    costs = {s["stage"]: stage_cost(s) for s in t.get("stages", [])}
    longest = max((ms for _, ms in rows), default=0) or 1

    out = ["", f" {st.bold('pgtrail')}  {st.dim('ACTUAL SPEED')}",
           "   " + st.dim(" ".join(t["meta"]["sql"].split())), ""]
    out.append(f"   {st.good(st.bold(f'{total:.3f} ms'))}  "
               + st.dim(f"statement {rt:.3f} ms + {end['command']} {commit:.3f} ms"))
    if t["result"]["error"]:
        out.append("   " + st.bad("error: " + t["result"]["error"].splitlines()[0]))
    out.append("")
    for name, ms in rows:
        bar = "█" * max(1, round(ms / longest * BAR)) if ms else ""
        flushed = name == "COMMIT" and any(
            r["resource_manager"] == "Transaction" and r["owner"] == "statement"
            for r in (t["wal"].get("records", []) if isinstance(t["wal"], dict) else []))
        note = {"wire+other": "  protocol, network, client",
                "COMMIT": "  waits for the WAL fsync to disk" if flushed else "  nothing to flush",
                "ROLLBACK": "  nothing to flush"}.get(name, "")
        if costs.get(name):
            note = "  " + costs[name]
        out.append(f"   {name:<10} {ms:7.3f} ms  {st.warn(bar)}{st.dim(note)}")
    count, parts = _steps(t)
    summary = f"{count:,} steps in {total:.3f} ms: " + ", ".join(parts) + "."
    lines = textwrap.wrap(summary, 84)
    head = f"{count:,} steps"
    out += ["", "   " + st.bold(lines[0][: len(head)]) + lines[0][len(head):]]
    out += ["   " + line for line in lines[1:]]
    waited = lock_wait(t)
    if waited:
        w = t["lock_wait"]["waited_ms"]
        out += textwrap.wrap(
            f"It {waited[0]} held by another session: without that wait, about {total - w:.3f} ms.",
            84, initial_indent="   ", subsequent_indent="   ")
    trees = t.get("trees") if isinstance(t.get("trees"), dict) else {}
    if trees.get("query") and trees.get("plan"):
        out.append(f"   Parse analysis built a query tree of {trees['query']['nodes']} nodes; the planner "
                   f"turned it into a plan tree of {trees['plan']['nodes']}.")
    for check in plan_checks(t):
        out += textwrap.wrap(check[0].upper() + check[1:] + ".", 84, initial_indent="   ",
                             subsequent_indent="   ")
    for depth, name, words in btree_lines(t):
        if depth == 0:
            out += textwrap.wrap(f"{name.lower()}: {words}.", 84, initial_indent="   ", subsequent_indent="   ")
    plans = considered(t)
    if len(plans) > 1:
        plan_ms = next((ms for name, ms in stages if name == "plan"), 0)
        worst = plans[-1]
        out += textwrap.wrap(
            f"The planner compared {len(plans)} plans in {plan_ms:.3f} ms and picked "
            f"{plan_name(plans[0])}; {plan_name(worst)} would have cost "
            f"{worst['ratio']:.1f}× as much.", 84, initial_indent="   ", subsequent_indent="   ")
    if total:
        if total < FRAME_MS:
            out.append(f"   That is about {FRAME_MS / total:,.0f} runs per screen refresh, "
                       f"{BLINK_MS / total:,.0f} per blink of an eye.")
        else:
            out.append(f"   That is about {total / FRAME_MS:,.0f} screen refreshes"
                       + (f", {total / BLINK_MS:,.1f} blinks of an eye." if total >= BLINK_MS else "."))
    files = touched_files(t)
    if files:
        out += ["", f"   {st.bold('Files touched')} {st.dim('(on the database server)')}"]
        out += [f"   {path}  {st.dim(what)}" for path, what in files]
    out.append("")
    out.append(st.dim("   measured on this server, with pgtrail's instrumentation on (a plain run is a "
                      "little faster);"))
    out.append(st.dim("   a network between client and server, or a slow disk, adds to it"))
    return "\n".join(out) + "\n"
