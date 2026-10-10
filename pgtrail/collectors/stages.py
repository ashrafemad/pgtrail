"""Layer 2: parse → analyze → rewrite → plan → execute.

The server reports each stage through log_parser_stats, log_planner_stats and
log_executor_stats, and auto_explain reports the executed plan. With
client_min_messages=log all of that arrives as notices on our own connection,
so no access to the server log file is needed.
"""
import json
import re

STAGES = {
    "PARSER STATISTICS": ("parse", "parser: SQL text → raw parse tree (syntax only, no catalog access)"),
    "PARSE ANALYSIS STATISTICS": ("analyze", "analyzer: names and types resolved against the catalogs → query tree"),
    "REWRITER STATISTICS": ("rewrite", "rewriter: rules and views expanded"),
    "PLANNER STATISTICS": ("plan", "planner: cheapest plan chosen from table statistics"),
    "EXECUTOR STATISTICS": ("execute", "executor: plan nodes run, through the access methods; pages read and modified"),
}
_ELAPSED = re.compile(r"([\d.]+) s user, ([\d.]+) s system, ([\d.]+) s elapsed")
_BLOCKS = re.compile(r"(\d+)/(\d+) \[\d+/\d+\] filesystem blocks in/out")
_FAULTS = re.compile(r"(\d+)/(\d+) \[\d+/\d+\] page faults/reclaims")


def parse_notices(notices):
    """notices: [(severity, primary, detail)] → (stages, plan_or_None)."""
    stages, plan = [], None
    for _sev, primary, detail in notices:
        primary, detail = primary or "", detail or ""
        if primary in STAGES:
            name, what = STAGES[primary]
            e, b, f = _ELAPSED.search(detail), _BLOCKS.search(detail), _FAULTS.search(detail)
            # Statements with RETURNING report the executor twice; the later
            # report covers the whole run, so it replaces the earlier one.
            stages = [s for s in stages if s["stage"] != name]
            stages.append(
                {
                    "stage": name,
                    "what": what,
                    "elapsed_ms": round(float(e[3]) * 1000, 4) if e else None,
                    "cpu_user_ms": round(float(e[1]) * 1000, 4) if e else None,
                    "cpu_system_ms": round(float(e[2]) * 1000, 4) if e else None,
                    # getrusage: 512-byte blocks that really went to/from the
                    # device (page-cache hits do not count).
                    "device_blocks_in": int(b[1]) if b else None,
                    "device_blocks_out": int(b[2]) if b else None,
                    "major_page_faults": int(f[1]) if f else None,
                    "minor_page_faults": int(f[2]) if f else None,
                }
            )
        elif primary.startswith("duration:") and "plan:" in primary:
            try:
                plan = json.loads(primary.split("plan:", 1)[1])
            except ValueError:
                plan = None
    return stages, plan
