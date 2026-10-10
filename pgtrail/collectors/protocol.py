"""Layer 1: the wire. Parses libpq's PQtrace output for one round trip."""
import re
from datetime import datetime

_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\t([FB])\t(\d+)\t(\w+)(?:\t(.*))?$"
)
_NOTICE_M = re.compile(r' M "((?:[^"\\\n]|\\.)*)')
_TX_STATE = {"I": "idle", "T": "in transaction", "E": "failed transaction"}
_PARSE_SQL = re.compile(r'^"(?:[^"\\]|\\.)*" "((?:[^"\\]|\\.)*)"')
# the extended protocol's messages, in plain words
_EXTENDED = {
    "Bind": "bind the parameters to the parsed statement",
    "Describe": "ask for the shape of the result (its columns)",
    "Execute": "run it",
    "Sync": "end of the batch: reply now",
    "ParseComplete": "parsed, analysed and rewritten",
    "BindComplete": "bound, and planned",
}


def _label(kind, body):
    body = body.strip()
    if kind == "NoticeResponse":
        return "pgtrail instrumentation (stage statistics, plan, trees); a normal client gets none"
    if kind == "ErrorResponse":
        m = _NOTICE_M.search(body)
        return m.group(1).replace('\\"', '"') if m else body[:80]
    if kind == "ReadyForQuery":
        return _TX_STATE.get(body.strip(), body)
    if kind == "RowDescription":
        return f"{body.split()[0]} column(s)" if body else ""
    if kind in ("Query", "CommandComplete"):
        return body.strip('"')
    if kind == "Parse":
        m = _PARSE_SQL.match(body)
        return "parse this statement: " + (m.group(1) if m else body)
    if kind in _EXTENDED:
        return _EXTENDED[kind]
    return ""


def parse_trace(text):
    """Return [{at_ms, dir, type, bytes, count, label}]; runs of DataRow and
    NoticeResponse are collapsed into one entry each."""
    raw = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            raw.append(
                {
                    "ts": datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S.%f"),
                    "dir": "client→server" if m[2] == "F" else "server→client",
                    "bytes": int(m[3]),
                    "type": m[4],
                    "body": m[5] or "",
                }
            )
        elif raw:
            raw[-1]["body"] += "\n" + line
    if not raw:
        return []
    t0 = raw[0]["ts"]
    out = []
    for r in raw:
        at = round((r["ts"] - t0).total_seconds() * 1000, 3)
        if out and r["type"] in ("DataRow", "NoticeResponse") and out[-1]["type"] == r["type"]:
            out[-1]["count"] += 1
            out[-1]["bytes"] += r["bytes"]
            out[-1]["wire_sizes"].append(r["bytes"] + 1)
            continue
        out.append(
            {
                "at_ms": at,
                "dir": r["dir"],
                "type": r["type"],
                "bytes": r["bytes"],
                "count": 1,
                "wire_sizes": [r["bytes"] + 1],  # length field + 1 type byte
                "label": _label(r["type"], r["body"]),
            }
        )
    return out
