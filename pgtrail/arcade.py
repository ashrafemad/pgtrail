"""The animation player (--theme, --pace) and the classic theme.

Like render.py, this reads the trace dict only. The animation runs on a
narrative clock (the real thing takes well under a millisecond); every number
on screen is the measured one.
"""
import os
import select
import shutil
import sys
import termios
import time
import textwrap
import tty


from .render import (TREE_TITLES, TREE_WORDS, considered, lock_wait, plan_name, plan_table,
                     request, stage_cost, tree_pages, tree_rows, why_chosen, no_index_words, btree_pages, wait_view)

W, H = 80, 24
FPS = 30
YELLOW, CYAN, MAGENTA, RED, GREEN, WHITE, BLUE, DIM = (
    "1;33", "1;36", "1;35", "1;31", "1;32", "1;37", "1;34", "2")
# --pace: (motion speed, how much longer to hold each step for reading)
PACES = {"normal": (1.0, 1.0), "slow": (0.95, 1.25), "slower": (0.65, 2.2)}
KEYS_HINT = " space next · p pause · q quit "
FULL_REPORT_HINT = "full text report: the same command without --theme and --pace"
STAGE_ORDER = ("parse", "analyze", "rewrite", "plan", "execute")
MAX_SLOTS = 8                          # catch and maze have room for 8
CLASSIC_SLOTS, CLASSIC_SLOT_W = 7, 11
TRACK_X0, TRACK_X1, TRACK_Y = 13, 29, 5


class ArcadeUnavailable(Exception):
    pass


class Canvas:
    """A grid of styled cells. Rows from 2 down are pushed down by `shift`, which
    makes room under the header for a query that needs more than one line."""

    def __init__(self, w=W, h=H, bg="", shift=0):
        self.w, self.h, self.bg, self.shift = w, h + shift, bg, shift
        self.cells = [[(" ", "")] * w for _ in range(self.h)]

    def put(self, x, y, text, style="", fixed=False):
        if y >= 2 and not fixed:
            y += self.shift
        for i, ch in enumerate(text):
            if 0 <= x + i < self.w and 0 <= y < self.h:
                self.cells[y][x + i] = (ch, style)

    def center(self, y, text, style="", x0=0, x1=None):
        x1 = self.w if x1 is None else x1
        self.put(x0 + max(0, (x1 - x0 - len(text)) // 2), y, text, style)

    def paged(self, x, y, width, rows, items, clock, mark="2", fixed=False):
        """Wrap [(text, style)] into `rows` rows. If it needs more, show one page
        at a time with "1/3" in the corner, so nothing is ever cut off."""
        lines = wrap_items(items, width)
        label = ""
        if len(lines) > rows:
            lines = wrap_items(items, width - 5)
            pages = [lines[i:i + rows] for i in range(0, len(lines), rows)]
            n = (clock // PAGE_FRAMES) % len(pages)
            lines, label = pages[n], f"{n + 1}/{len(pages)}"
        for i, (line, style) in enumerate(lines):
            self.put(x, y + i, line, style, fixed)
        if label:
            self.put(x + width - len(label), y, label, mark, fixed)

    def box(self, x, y, w, h, style="", title=""):
        self.put(x, y, "┌" + "─" * (w - 2) + "┐", style)
        for row in range(y + 1, y + h - 1):
            self.put(x, row, "│", style)
            self.put(x + 1, row, " " * (w - 2))
            self.put(x + w - 1, row, "│", style)
        self.put(x, y + h - 1, "└" + "─" * (w - 2) + "┘", style)
        if title:
            self.put(x + 2, y, f" {title} ", style)

    def to_text(self):
        return "\n".join("".join(ch for ch, _ in row).rstrip() for row in self.cells)

    def to_ansi(self):
        lines = []
        for row in self.cells:
            out, current = [], None
            for ch, style in row:
                if style != current:
                    sgr = ";".join(s for s in (self.bg, style) if s)
                    out.append("\033[0m" + (f"\033[{sgr}m" if sgr else ""))
                    current = style
                out.append(ch)
            lines.append("".join(out) + "\033[0m\033[K")
        return "\n".join(lines)


PAGE_FRAMES = 105                      # 3.5 s per page of text that does not fit


def wrap_items(items, width):
    """[(text, style)] → [(line, style)], wrapped; continuation lines indented."""
    out = []
    for text, style in items:
        lines = textwrap.wrap(text, width, subsequent_indent="  ") if text else [""]
        out += [(line, style) for line in lines]
    return out


def sql_canvas(trace, w, h, bg, extra_rows, frame, style):
    """A canvas whose header shows the whole query, wrapped, using up to
    extra_rows spare terminal rows; whatever still does not fit is paged."""
    sql = " ".join(trace["meta"]["sql"].split())
    rows = min(len(wrap_items([(sql, style)], w - 2)), 1 + max(0, extra_rows))
    c = Canvas(w, h, bg, shift=rows - 1)
    c.paged(1, 1, w - 2, rows, [(sql, style)], frame, style, fixed=True)
    return c


def _width(text):
    """Length of a box line: a string, or a list of (piece, style)."""
    return sum(len(p) for p, _ in text) if isinstance(text, list) else len(text)


def name_lines(name, width, clock=0):
    """A relation name in two lines, split after underscores where possible. A
    longer name shows two lines at a time, in turn, so all of it is seen."""
    lines = []
    while len(name) > width:
        cut = name.rfind("_", 0, width)
        cut = cut + 1 if cut >= width // 2 else width
        lines.append(name[:cut])
        name = name[cut:]
    lines.append(name)
    if len(lines) <= 2:
        return (lines + [""])[:2]
    pages = [lines[i:i + 2] for i in range(0, len(lines), 2)]
    return (pages[(clock // PAGE_FRAMES) % len(pages)] + [""])[:2]


def relative(path, trace):
    """A data file path relative to the data directory, which is long and the same for all."""
    root = trace["meta"].get("data_directory")
    return path[len(root) + 1:] if root and path and path.startswith(root + "/") else path


class Arcade:
    skip = False                       # set by the player when space is pressed
    detail = "full"                    # --detail: summary, detailed or full
    extra_rows = 0                     # terminal rows beyond H, set by the player

    def __init__(self, trace, reading=1.0):
        self.t = trace
        self.reading = reading
        self.frame = 0
        self.caption = ""
        self.time_ms = 0.0
        self.wal_bytes = 0
        self.locks = []
        self.locks_taken = 0
        self.stages = {s["stage"]: s for s in trace.get("stages", [])}
        self.stage_state = {}          # name → "active" | "done"
        self.slots = []                # {rel, block, state}
        self.tape = []                 # {text, flushed}
        self.packet = None             # (x, char, label, sublabel, eaten_from, eaten_to)
        self.sprite = None             # (x, y, char, style)
        self.overlay = None            # list of (text, style)
        self.disk_flash = 0

    # ---- drawing ---------------------------------------------------------
    def draw(self):
        c = sql_canvas(self.t, W, H, "", self.extra_rows, self.frame, DIM)
        self.frame += 1
        blink = (self.frame // 5) % 2 == 0

        c.put(1, 0, "PGTRAIL", YELLOW)
        c.put(12, 0, f"TIME {self.time_ms:8.3f} ms", WHITE)
        c.put(32, 0, f"PAGES {len(self.slots):02d}", CYAN)
        c.put(43, 0, f"WAL {self.wal_bytes:06d} B", MAGENTA)
        c.put(59, 0, f"LOCKS {len(self.locks):02d}", GREEN)

        c.box(1, 3, 12, 5, BLUE)
        c.center(5, "CLIENT", WHITE, 1, 13)
        c.box(30, 3, 49, 5, BLUE, "BACKEND")
        for i, name in enumerate(STAGE_ORDER):
            x0, state = 31 + i * 9 + (i > 0), self.stage_state.get(name)
            style = DIM if state is None else (YELLOW if state == "active" and blink else
                                               WHITE if state == "active" else GREEN)
            c.center(4, name.upper(), style, x0, x0 + 9)
            if state == "done" and name in self.stages:
                c.center(5, f"{self.stages[name]['elapsed_ms']:.3f}", GREEN, x0, x0 + 9)
        if self.locks:
            by_mode = {}
            for target, mode in self.locks:
                by_mode.setdefault(mode, []).append(target)
            text = "LOCKS " + " · ".join(f"{mode} {', '.join(t)}" for mode, t in by_mode.items())
            c.paged(32, 6, 45, 1, [(text, GREEN)], self.frame, DIM)

        for x in range(TRACK_X0, TRACK_X1 + 1):
            c.put(x, TRACK_Y, "·", DIM)
        if self.packet:
            x, ch, label, sub, lo, hi = self.packet
            for ex in range(lo, hi + 1):
                c.put(ex, TRACK_Y, " ")
            c.put(x, TRACK_Y, ch, YELLOW)
            c.center(TRACK_Y - 1, label[:17], YELLOW, TRACK_X0, TRACK_X1 + 1)
            c.center(TRACK_Y + 1, sub[:17], DIM, TRACK_X0, TRACK_X1 + 1)

        c.put(1, 9, "SHARED BUFFERS", CYAN)
        c.put(17, 9, "in memory", DIM)
        for i, slot in enumerate(self.slots[:CLASSIC_SLOTS]):
            x = 1 + i * CLASSIC_SLOT_W
            style = {"read": YELLOW, "hit": GREEN, "dirty": RED, "soft": MAGENTA,
                     "clean": GREEN}[slot["state"]]
            if slot.get("flash", 0) > 0:
                slot["flash"] -= 1
                style = WHITE if blink else style
            if slot["state"] in ("dirty", "soft") and slot.get("alarm") and not blink:
                style = DIM
            w = CLASSIC_SLOT_W
            c.box(x, 10, w, 5, style)
            first, second = name_lines(slot["base"], w - 2, self.frame)
            c.put(x + 1, 11, first, style)
            c.put(x + 1, 12, second, style)
            where = (f"block {slot['block']}" if slot["fork"] == "main"
                     else f"{slot['fork']} blk {slot['block']}")
            c.put(x + 1, 13, where[: w - 2], style)
            tag = {"read": "READ IN", "hit": "HIT", "dirty": "DIRTY", "soft": "DIRTY*",
                   "clean": "CLEAN"}[slot["state"]]
            c.center(14, f" {tag} ", style, x, x + w)
        if len(self.slots) > CLASSIC_SLOTS:
            c.put(78, 12, f"+{len(self.slots) - CLASSIC_SLOTS}", DIM)

        disk_style = WHITE if self.disk_flash > 0 and blink else BLUE
        self.disk_flash = max(0, self.disk_flash - 1)
        c.box(1, 18, 78, 3, disk_style, "DISK: DATA FILES")
        files = []
        for s in self.slots:
            entry = f"{relative(s['file'], self.t)} {s['rel']}" if s.get("file") else s["rel"]
            if entry not in files:
                files.append(entry)
        c.paged(3, 19, 74, 1, [(" · ".join(files), DIM)], self.frame, DIM)

        c.put(1, 21, "WAL", MAGENTA)
        x = 5
        for i, rec in enumerate(self.tape):
            style = GREEN if rec["flushed"] else YELLOW
            cell = f"[{rec['text']}]"
            if x + len(cell) > 58:
                c.put(x, 21, f"+{len(self.tape) - i} more", DIM)
                break
            c.put(x, 21, cell, style)
            x += len(cell)
        if self.tape:
            flushed = all(r["flushed"] for r in self.tape)
            c.put(67, 21, "ON DISK" if flushed else "IN MEMORY", GREEN if flushed else YELLOW)

        if self.sprite:
            x, y, ch, style = self.sprite
            c.put(x, y, ch, style)
        c.paged(1, 22, 78, 2, [("► " + self.caption, WHITE)], self.frame, DIM)

        if self.overlay:
            h = len(self.overlay) + 4
            w = max(_width(t) for t, _ in self.overlay) + 8
            x0, y0 = (W - w) // 2, (H - h) // 2
            c.box(x0, y0, w, h, YELLOW)
            for i, (text, style) in enumerate(self.overlay):
                if isinstance(text, list):      # a line in colored pieces: (text, style)
                    x = x0 + (w - _width(text)) // 2
                    for piece, piece_style in text:
                        c.put(x, y0 + 2 + i, piece, piece_style or style)
                        x += len(piece)
                else:
                    c.center(y0 + 2 + i, text, style, x0, x0 + w)
        return c

    def hold(self, frames):
        # captions are read during holds, so the pace stretches them
        for _ in range(round(frames * self.reading)):
            if self.skip:
                break
            yield self.draw()
        self.skip = False

    # ---- moves -----------------------------------------------------------
    def fly(self, to_backend, label, sub):
        xs = range(TRACK_X0, TRACK_X1 + 1) if to_backend else range(TRACK_X1, TRACK_X0 - 1, -1)
        for x in xs:
            eaten = (TRACK_X0, x) if to_backend else (x, TRACK_X1)
            self.packet = (x, "►" if to_backend else "◄", label, sub, *eaten)
            yield self.draw()
        self.packet = None
        yield from self.hold(4)

    def slot_x(self, index):
        return 1 + min(index, CLASSIC_SLOTS - 1) * CLASSIC_SLOT_W + CLASSIC_SLOT_W // 2

    def travel(self, index, down, style):
        rows = range(15, 18) if down else range(17, 14, -1)
        for y in rows:
            self.sprite = (self.slot_x(index), y, "■", style)
            yield from self.hold(2)
        self.sprite = None

    # ---- the story -------------------------------------------------------
    def run(self):
        t = self.t
        result, proto, end = t["result"], t["protocol"], t["end"]

        self.overlay = [("READY!", YELLOW)]
        self.caption = "one statement, start to finish"
        yield from self.hold(24)
        self.overlay = None

        label, size, words = request(t)
        self.caption = f"the client sends {words} ({size} bytes)"
        yield from self.fly(True, label, f"{size} B")

        for name in STAGE_ORDER:
            if name not in self.stages:
                continue
            self.stage_state[name] = "active"
            cost = stage_cost(self.stages[name])
            self.caption = f"{name.upper()}: {self.stages[name]['what']}" + (f" · {cost}" if cost else "")
            if name == (wait_view(self.t) or {}).get("stage"):
                yield from self.wait_scene()
            if name == "execute":
                yield from self.tree_scene("btree")
                yield from self.execute()
            else:
                yield from self.hold(14)
            if name == "analyze":
                yield from self.tree_scene("query")
            if name == "plan":
                yield from self.plan_scene()
                yield from self.tree_scene("plan")
            self.stage_state[name] = "done"
            self.time_ms += self.stages[name]["elapsed_ms"] or 0
            yield from self.hold(4)

        if result["error"]:
            self.caption = "ERROR: " + result["error"].splitlines()[0]
            yield from self.fly(False, "ERROR", "")
        else:
            rows = f"{result['row_count']} row(s)" if result["columns"] else ""
            self.caption = f"the answer goes back: {result['status']} {rows}".rstrip()
            yield from self.fly(False, (result["status"] or "DONE").upper(), rows)
        self.time_ms = proto["round_trip_ms"]
        yield from self.hold(10)

        yield from self.finish(end)
        yield from self.aftermath()

        b = t.get("buffers", {}).get("summary", {})
        self.caption = FULL_REPORT_HINT
        self.overlay = [
            ("STATEMENT FAILED" if result["error"] else "STATEMENT CLEAR!", RED if result["error"] else YELLOW),
            ("", ""),
            (f"ROUND TRIP   {proto['round_trip_ms']:9.3f} ms", WHITE),
            (f"{end['command']:<10}   {end['round_trip_ms']:9.3f} ms", WHITE),
            (f"PAGES READ IN       {b.get('loaded', 0):5d}", CYAN),
            (f"PAGES MODIFIED      {b.get('modified', 0):5d}", RED),
            (f"WAL BYTES        {self.wal_bytes:8d}", MAGENTA),
            (f"LOCKS TAKEN         {self.locks_taken:5d}", GREEN),
        ]
        yield from self.hold(60)

    def wait_scene(self):
        """Another session at the same time: a wait, no wait, or a deadlock."""
        v = wait_view(self.t)
        if not v:
            return
        frames = 60
        for i in range(1, frames + 1):
            box = [(v["title"], RED if v["blocked"] else GREEN), ("", "")] + [(line, WHITE) for line in v["box"]]
            if v["blocked"]:
                box += [("", ""), (f"waiting {v['waited'] * i / frames:8.1f} ms", YELLOW),
                        ("█" * (i // 3) + "·" * (20 - i // 3), YELLOW)]
            self.overlay = box
            self.caption = v["words"]
            yield self.draw()
        if v["blocked"]:
            self.overlay[0] = ("DEADLOCK BROKEN" if v["kind"] == "deadlock" else "LOCK GRANTED", GREEN)
        lines = len(wrap_items([("► " + v["words"], "")], 78))
        yield from self.hold(max(90, -(-lines // 2) * PAGE_FRAMES))
        self.overlay = None

    def tree_scene(self, kind):
        """The server's own tree, a page at a time. summary shows no trees;
        detailed shows only the B-tree dive."""
        if self.detail == "summary" or (self.detail == "detailed" and kind != "btree"):
            return
        pages = tree_pages(self.t, kind, 60, 13)
        if kind == "btree":
            pages = [[[(text, GREEN if on else WHITE) for text, on in parts] for parts in page]
                     for page in btree_pages(self.t, 60, 13)]
        if kind == "btree" and no_index_words(self.t):
            self.caption = no_index_words(self.t)
            yield from self.hold(60)
        if not pages or not pages[0]:
            return
        nodes = len(tree_rows(self.t, kind))    # the steps shown, not the server's full count
        width = max(_width(line) for page in pages for line in page)
        for n, page in enumerate(pages, 1):
            where = f" · page {n}/{len(pages)}" if len(pages) > 1 else ""
            self.overlay = [(TREE_TITLES[kind].upper(), YELLOW), (f"{nodes} {'steps' if kind == 'btree' else 'nodes'}{where}", DIM),
                            ("", "")] + [(line if isinstance(line, list) else line.ljust(width), WHITE) for line in page]
            self.caption = f"{kind.upper()} TREE: {TREE_WORDS[kind]}"
            yield from self.hold(60)
        self.overlay = None

    def plan_scene(self):
        """The candidates the planner compared, then the cheapest one wins."""
        if self.detail == "summary":
            return
        plans = considered(self.t)
        if len(plans) < 2:
            return
        table = plan_table(plans, 50)
        head = [("PLANS CONSIDERED", YELLOW), ("estimated cost", DIM), ("", "")]
        for shown in range(1, len(plans) + 1):
            self.overlay = head + [(f"  {text}", WHITE) for text, i, _ in table if i < shown]
            p = plans[shown - 1]
            self.caption = f"PLAN: candidate {shown} of {len(plans)}, estimated cost {p['total_cost']:,.2f}"
            yield from self.hold(18)
        self.overlay = head + [
            (("► " if first and i == 0 else "  ") + text, GREEN if i == 0 else DIM)
            for text, i, first in table]
        width = len(self.overlay[-1][0])   # left-align the why under the table
        self.overlay += [("", "")] + [(f"  {line}".ljust(width), WHITE) for line in textwrap.wrap(
            "why: " + why_chosen(self.t), width - 2)]
        self.caption = f"PLAN: the cheapest wins: {plan_name(plans[0])}"
        yield from self.hold(60)
        self.overlay = None

    def execute(self):
        t = self.t
        for lock in t["locks"] if isinstance(t["locks"], list) else []:
            if lock["locktype"] != "relation":
                continue
            self.locks.append((lock["target"], lock["mode"].replace("Lock", "")))
            self.locks_taken += 1
            self.caption = f"LOCK MANAGER: {lock['mode']} on {lock['target']} ({lock['why']})"
            yield from self.hold(10)

        groups = t["buffers"].get("relations", []) if isinstance(t["buffers"], dict) else []
        pages = [(g, p) for g in groups for p in g["pages"]]
        # indexes are read before the heap pages they point to
        pages.sort(key=lambda gp: (gp[0]["system"], gp[0]["kind"] != "index"))
        for g, p in pages:
            name = g["name"] + ("" if g["fork"] == "main" else f"[{g['fork']}]")
            slot = {"rel": name, "base": g["name"], "fork": g["fork"], "block": p["block"],
                    "file": p.get("file") or g.get("file"), "state": "hit", "flash": 0}
            where = relative(slot["file"], self.t) if slot["file"] else "the OS"
            index = len(self.slots)
            if p["loaded"]:
                self.caption = f"BUFFER MANAGER · MISS: {name} block {p['block']}, read from {where}"
                self.disk_flash = 6
                yield from self.travel(index, False, YELLOW)
                slot["state"] = "read"
            else:
                self.caption = f"BUFFER MANAGER · HIT: {name} block {p['block']} is already in memory"
            slot["flash"] = 8
            self.slots.append(slot)
            yield from self.hold(10)
            if p["modified"]:
                slot.update(state="dirty", flash=8)
                self.caption = f"MODIFY: {name} block {p['block']} changed in memory only, now dirty"
                yield from self.hold(12)
            elif p["dirtied"]:
                slot.update(state="soft", flash=8)
                self.caption = f"DIRTY*: {name} block {p['block']} dirtied without a WAL record of its own"
                yield from self.hold(12)

        wal = t["wal"].get("records", []) if isinstance(t["wal"], dict) else []
        for rec in wal:
            if rec["resource_manager"] == "Transaction" or (
                    rec["owner"] != "statement" and not rec["blocks"]):
                continue
            self.tape.append({"text": f"{rec['record_type']} {rec['bytes']}B", "flushed": False})
            self.wal_bytes += rec["bytes"]
            fpi = " (full-page image)" if rec["fpi_bytes"] else ""
            self.caption = (f"WAL: {rec['resource_manager']}/{rec['record_type']} record, "
                            f"{rec['bytes']} bytes{fpi}, still in memory")
            yield from self.hold(12)

    def finish(self, end):
        t = self.t
        cmd = end["command"]
        self.caption = f"the client sends {cmd}"
        yield from self.fly(True, cmd, "")
        wal = t["wal"].get("records", []) if isinstance(t["wal"], dict) else []
        for rec in wal:
            if rec["resource_manager"] == "Transaction" and rec["owner"] == "statement":
                self.tape.append({"text": f"{rec['record_type']} {rec['bytes']}B", "flushed": False})
                self.wal_bytes += rec["bytes"]
                yield from self.hold(8)
        if cmd == "COMMIT" and self.tape:
            self.caption = ("TRANSACTION MANAGER: COMMIT waits for the WAL fsync, the durability point; "
                            "then pg_xact marks it committed")
            self.disk_flash = 12
            yield from self.hold(12)
            for rec in self.tape:
                rec["flushed"] = True
            yield from self.hold(10)
        elif cmd == "ROLLBACK":
            self.caption = "ROLLBACK: nothing to wait for, the abort record is flushed lazily"
            yield from self.hold(16)
        self.caption = f"{cmd} confirmed after {end['round_trip_ms']:.3f} ms"
        yield from self.fly(False, cmd, f"{end['round_trip_ms']:.3f} ms")
        self.locks = []

    def aftermath(self):
        a = self.t.get("aftermath")
        dirty = [(i, s) for i, s in enumerate(self.slots) if s["state"] in ("dirty", "soft")]
        if not a or not dirty:
            return
        for _, s in dirty:
            s["alarm"] = True
        self.caption = (f"{len(dirty)} page(s) still dirty: the data files on disk have NOT been "
                        "written yet")
        yield from self.hold(40)
        if "checkpoint_ms" in a:
            for i, s in dirty:
                target = relative(s["file"], self.t) if s.get("file") else "its data file"
                self.caption = f"CHECKPOINT: writes {s['rel']} block {s['block']} to {target}"
                yield from self.travel(i, True, RED)
                self.disk_flash = 6
                s.update(state="clean", alarm=False, flash=6)
                yield from self.hold(8)
            self.caption = f"CHECKPOINT done in {a['checkpoint_ms']:.1f} ms: data files and WAL agree"
            yield from self.hold(24)
        else:
            self.caption = "the checkpointer will write them later (try --checkpoint)"
            yield from self.hold(30)


def play(trace, out=sys.stdout, story=None, pace="normal", detail="full"):
    """Show the frames. Keys while it plays: space skips to the next step,
    p pauses and resumes, q quits."""
    if not out.isatty():
        raise ArcadeUnavailable("the animation needs an interactive terminal")
    story = story or Arcade
    w, h = getattr(story, "W", W), getattr(story, "H", H)
    size = shutil.get_terminal_size()
    if size.columns < w or size.lines < h:
        raise ArcadeUnavailable(f"this animation needs a terminal of at least {w}x{h} "
                                f"(this one is {size.columns}x{size.lines})")
    motion, reading = PACES[pace]
    delay = 1 / (FPS * motion)
    game = story(trace, reading)
    game.extra_rows = size.lines - h   # room to show a long query in full
    game.detail = detail
    keys = sys.stdin.fileno() if sys.stdin.isatty() else None
    saved = termios.tcgetattr(keys) if keys is not None else None
    last, paused = None, False
    out.write("\033[?1049h\033[?25l\033[2J")      # alternate screen, hide cursor
    try:
        if keys is not None:
            tty.setcbreak(keys)
        frames = game.run()
        while True:
            if not paused:
                canvas = next(frames, None)
                if canvas is None:
                    break
                out.write("\033[H" + canvas.to_ansi())
                out.flush()
                last = canvas
            ready = select.select([keys], [], [], delay)[0] if keys is not None else time.sleep(delay)
            if ready:
                key = os.read(keys, 32).decode("utf-8", "replace").lower()
                if "q" in key:
                    break
                if "p" in key:
                    paused = not paused
                if " " in key:
                    game.skip, paused = True, False
    except KeyboardInterrupt:
        pass
    finally:
        if saved is not None:
            termios.tcsetattr(keys, termios.TCSADRAIN, saved)
        out.write("\033[0m\033[?25h\033[?1049l")  # restore screen and cursor
        out.flush()
    if last:
        out.write(last.to_ansi() + "\n")          # leave the final frame behind
