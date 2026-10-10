"""Layer 7: the operating system. Attaches strace to the backend (and to the
checkpointer, background writer and WAL writer) and translates every file and
socket syscall back into database terms.

Needs strace on the database host and permission to ptrace the postgres
processes (root, or the postgres OS user; in Docker: --cap-add=SYS_PTRACE).
"""
import os
import re
import select
import shutil
import subprocess
import tempfile
import time
from datetime import datetime

TRACED = (
    "read,write,pread64,pwrite64,preadv,pwritev,readv,writev,fsync,fdatasync,"
    "sync_file_range,openat,lseek,recvfrom,sendto,ftruncate,fallocate,unlink,rename"
)
_LINE = re.compile(r"^(\d+)\s+(\d\d):(\d\d):(\d\d\.\d+) (.*)$")
_RET = r"\s+= (-?\d+|\?)(?:<([^>]*)>)?(?: \w+ \([^)]*\))?(?: <([\d.]+)>)?$"
_CALL = re.compile(r"^(\w+)\((.*)\)" + _RET)
_UNFINISHED = re.compile(r"^(\w+)\((.*) <unfinished \.\.\.>$")
_RESUMED = re.compile(r"^<\.\.\. (\w+) resumed>(.*)\)" + _RET)
_FD_PATH = re.compile(r"^\d+<([^>]*)>")
_REL_FILE = re.compile(r"(?:^|/)base/(\d+)/(\d+)(?:_(fsm|vm|init))?(?:\.(\d+))?$")
_QUOTED = re.compile(r'"([^"]+)"')
_GLOBAL_FILE = re.compile(r"(?:^|/)global/(\d+)(?:_(fsm|vm|init))?$")


class StraceUnavailable(Exception):
    pass


def _seconds(h, m, s):
    return int(h) * 3600 + int(m) * 60 + float(s)


def now_seconds():
    n = datetime.now()
    return n.hour * 3600 + n.minute * 60 + n.second + n.microsecond / 1e6


class Strace:
    def __init__(self, roles):
        self.roles = roles  # {pid: "backend" | "checkpointer" | ...}
        self.proc = None
        self.out = None

    def start(self, timeout=3.0):
        if not shutil.which("strace"):
            raise StraceUnavailable("strace is not installed on this machine")
        missing = [p for p in self.roles if not os.path.exists(f"/proc/{p}")]
        if missing:
            raise StraceUnavailable(
                "the postgres processes are not visible here; --os only works on the database host"
            )
        self.out = tempfile.NamedTemporaryFile("r", suffix=".strace", delete=False)
        pids = ",".join(str(p) for p in self.roles)
        self.proc = subprocess.Popen(
            ["strace", "-f", "-tt", "-T", "-y", "-s", "0", "-e", "trace=" + TRACED,
             "-o", self.out.name, "-p", pids],
            stderr=subprocess.PIPE,
        )
        # Read the raw fd: a buffered readline() could swallow several
        # "attached" lines at once and leave select() waiting for nothing.
        fd, log, deadline = self.proc.stderr.fileno(), "", time.monotonic() + timeout
        while log.count(" attached") < len(self.roles) and time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                log += os.read(fd, 65536).decode("utf-8", "replace")
        if log.count(" attached") < len(self.roles):
            self.proc.kill()
            raise StraceUnavailable(
                "could not attach (" + (log.strip().splitlines() or ["no output"])[-1] + "). "
                "Run as root or as the postgres OS user; in Docker add --cap-add=SYS_PTRACE"
            )

    def stop(self):
        time.sleep(0.05)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        with open(self.out.name) as f:
            text = f.read()
        os.unlink(self.out.name)
        return text


# Bytes a TLS record adds around each protocol message (1.3, then 1.2 AES-GCM).
_TLS_OVERHEADS = (22, 29)


def _describe(name, args, ret, ret_path, rels, block_size, ours, notice_sizes, tls):
    """Translate one syscall into (target, meaning, touches_our_pages)."""
    m = _FD_PATH.match(args)
    path = ret_path or (m.group(1) if m else "")
    if not path:
        quoted = _QUOTED.search(args)
        path = quoted.group(1) if quoted else ""
    nbytes = int(ret) if ret.lstrip("-").isdigit() else 0
    last = args.rsplit(", ", 1)[-1]
    offset = int(last) if last.isdigit() else None

    if path.startswith("socket:") or name in ("recvfrom", "sendto"):
        unit = "TLS record" if tls else "message"
        if name in ("recvfrom", "read", "readv"):
            what = f"{unit} header" if nbytes == 5 else f"{unit} body"
            return "client socket", f"receive {what} from client, {nbytes} bytes", False
        plain = [nbytes - o for o in (_TLS_OVERHEADS if tls else (0,))]
        note = " (pgtrail instrumentation notice)" if any(p in notice_sizes for p in plain) else ""
        return "client socket", f"send to client, {nbytes} bytes{note}", False

    if "/pg_wal/" in path or path.startswith("pg_wal/"):
        seg = path.rsplit("/", 1)[-1]
        if name in ("fdatasync", "fsync"):
            return f"WAL {seg}", "flush WAL to stable storage (the durability point)", False
        if name.startswith("pwrite"):
            return f"WAL {seg}", f"write {nbytes} bytes of WAL at offset {offset}", False
        return f"WAL {seg}", {"openat": "open WAL segment"}.get(name, name), False

    rel, fork, seg = None, "main", 0
    m = _REL_FILE.search(path)
    if m:
        rel = rels.by_filenode(int(m[1]), 0, int(m[2]))
        fork, seg = m[3] or "main", int(m[4] or 0)
    else:
        m = _GLOBAL_FILE.search(path)
        if m:
            rel = rels.by_filenode(0, 1664, int(m[1]))
            fork = m[2] or "main"
    if rel:
        target = rel["name"] + ("" if fork == "main" else f" [{fork}]")
        block = None if offset is None else offset // block_size + seg * (2**30 // block_size)
        mine = (rel["name"], fork, block) in ours
        if name.startswith("pread") or name == "read":
            return target, f"read block {block} from the OS (page cache or disk)", mine
        if name.startswith("pwrite") or name == "write":
            return target, f"write block {block} to the OS page cache", mine
        if name in ("fsync", "fdatasync"):
            mine = any(r == rel["name"] and f == fork for r, f, _ in ours)
            return target, "force this data file to stable storage", mine
        if name == "lseek":
            return target, f"ask the file size: {nbytes // block_size} block(s)", False
        if name == "openat":
            return target, "open the data file", False
        return target, name, False
    if "pg_xact" in path:
        return "pg_xact", "commit status (clog): " + name, False
    return (path.rstrip("/").rsplit("/", 1)[-1] or "?"), name, False


def parse(text, roles, phases, rels, block_size=8192, ours=(), notice_sizes=(), tls=False):
    """phases: [(name, start_s, end_s)] in seconds since local midnight.

    ours: {(relation, fork, block)} modified by the statement, used to pick its
    pages out of everything a checkpoint writes.
    """
    events, pending = [], {}
    first = phases[0][1] if phases else 0
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        pid, at, rest = int(m[1]), _seconds(m[2], m[3], m[4]), m[5]
        u = _UNFINISHED.match(rest)
        if u:
            pending[pid] = (at, u[1], u[2])
            continue
        r = _RESUMED.match(rest)
        if r and pid in pending:
            at, name, args = pending.pop(pid)
            args, ret, ret_path, took = args + r[2], r[3], r[4], r[5]
        else:
            c = _CALL.match(rest)
            if not c:
                continue
            name, args, ret, ret_path, took = c[1], c[2], c[3], c[4], c[5]
        if at < first or "EAGAIN" in rest or "anon_inode:" in args:
            continue
        phase = next((p for p, s, e in phases if s <= at <= e), "between phases")
        target, meaning, mine = _describe(name, args, ret, ret_path, rels, block_size,
                                          ours, notice_sizes, tls)
        events.append(
            {
                "at_ms": round((at - first) * 1000, 3),
                "phase": phase,
                "process": roles.get(pid, str(pid)),
                "syscall": name,
                "target": target,
                "meaning": meaning,
                "statement_page": mine,
                "took_ms": round(float(took) * 1000, 3) if took else None,
            }
        )
    events.sort(key=lambda e: e["at_ms"])
    return events
