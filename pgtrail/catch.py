"""Catch theme: the arcade replay told as a monster-catching adventure.

Same trace and same numbers as arcade.py; only the story changes. Pages are
wild creatures, shared_buffers is your party, the data files are the tall
grass and the WAL is the save file. Every ball color stands for one kind of
event, and every line in the text box has a plain explanation under it.
"""
import math

from .arcade import (_width, FULL_REPORT_HINT, KEYS_HINT, MAX_SLOTS, PAGE_FRAMES, STAGE_ORDER, name_lines,
                     relative, sql_canvas, wrap_items)
from .render import (TREE_TITLES, TREE_WORDS, considered, lock_wait, plan_name, plan_table,
                     request, stage_cost, tree_pages, tree_rows, why_chosen, no_index_words, btree_pages, wait_view)
from .palette import BG, DIM, EXPLAIN, DIRTY, DISK, GOLD, HIT, QUERY, READ, TEXT, WAL, rgb

FRAME = rgb(96, 140, 210, "1;34")
CAP = rgb(225, 60, 50, "1;31")
SKIN = rgb(245, 205, 160, "1;33")
GRASS = rgb(95, 180, 85, "1;32")
GRASS_DARK = rgb(50, 115, 55, "32", bold=False)
LEGEND = (("query", QUERY), ("disk read", READ), ("cache hit", HIT),
          ("modified", DIRTY), ("WAL", WAL), ("to disk", DISK))

SPIN = "◐◓◑◒"
GRASS_CHARS = ",;'`,;\"'"
TYPE_SPEED = 3                          # text box characters per frame
READ_FRAMES_PER_WORD = 6                # at 30 fps: about 5 words a second
SLOT_STYLE = {"read": READ, "hit": HIT, "dirty": DIRTY, "soft": DIRTY, "clean": DISK}
SLOT_TAG = {"read": "NEW", "hit": "HIT", "dirty": "DRT", "soft": "DRT*", "clean": "OK"}

# layout of the 100x30 screen
TRACK_X0, TRACK_X1, TRACK_Y = 16, 36, 5
BACKEND_X = 38
SLOT_X0, SLOT_W, SLOT_Y, SLOT_H = 2, 12, 12, 7
MAX_ANIMATED_WAL = 6             # WAL records shown one by one; the rest in one line
GRASS_Y = 21
SAVE_Y = 24
TEXT_Y = 25


class CatchGame:
    W, H = 100, 30
    name = "catch"

    def __init__(self, trace, reading=1.0):
        self.t = trace
        self.extra_rows = 0            # terminal rows beyond H, set by the player
        self.detail = "full"           # --detail: summary, detailed or full
        self.skip = False              # set by the player when space is pressed
        self.reading = reading         # --pace: how long each step stays up to be read
        self.frame = 0
        self.time_ms = 0.0
        self.wal_bytes = 0
        self.stages = {s["stage"]: s for s in trace.get("stages", [])}
        self.stage_state = {}          # name → "active" | "done"
        self.badges = []               # relation locks currently held
        self.badges_earned = 0
        self.slots = []                # {rel, block, state, usage}
        self.tape = []                 # {text, saved}
        self.balls = []                # (x, y, glyph, style)
        self.track = None              # (label, sublabel, style) around the wire
        self.qball = None              # x of the query ball inside the backend, under its stage
        self.fun = self.plain = ""
        self.typed = 0
        self.overlay = None
        self.rustle = 0
        self.throwing = False

    # ---- drawing ---------------------------------------------------------
    def draw(self):
        c = sql_canvas(self.t, self.W, self.H, BG, self.extra_rows, self.frame, DIM)
        self.frame += 1
        self.typed += TYPE_SPEED
        blink = (self.frame // 5) % 2 == 0

        c.put(1, 0, "PGTRAIL", CAP)
        c.put(11, 0, f"TIME {self.time_ms:8.3f} ms", TEXT)
        c.put(33, 0, f"PARTY {len(self.slots):02d}", HIT)
        c.put(46, 0, f"SAVE {self.wal_bytes:06d} B", WAL)
        n = len(self.badges)
        c.put(64, 0, "BADGES " + ("◆" * n if n <= 8 else f"◆×{n}"), GOLD)

        c.box(1, 3, 14, 6, FRAME, "YOU")
        c.put(5, 4, "▄██▄▄", CAP)
        c.put(5, 5, "(•_•)", SKIN)
        if self.throwing:
            c.put(10, 5, "╯", SKIN)
            c.put(6, 6, "/█", TEXT)
        else:
            c.put(6, 6, "/█\\", TEXT)
        c.center(7, "client", DIM, 1, 15)

        c.box(BACKEND_X, 3, 61, 6, FRAME, "BACKEND · server")
        for i, name in enumerate(STAGE_ORDER):
            x0, state = BACKEND_X + 2 + i * 11, self.stage_state.get(name)
            style = DIM if state is None else (GOLD if state == "active" and blink else
                                               TEXT if state == "active" else HIT)
            c.center(4, name.upper(), style, x0, x0 + 11)
            if state == "done" and self.stages[name].get("elapsed_ms") is not None:
                c.center(5, f"{self.stages[name]['elapsed_ms']:.3f} ms", HIT, x0, x0 + 11)
        if self.badges:
            text = "  ".join(f"◆ {rel} {mode}" for rel, mode in self.badges)
            c.paged(BACKEND_X + 2, 7, 57, 1, [(text, GOLD)], self.frame, DIM)
        if self.qball is not None:
            c.put(self.qball, 6, SPIN[(self.frame // 4) % 4], QUERY)

        for x in range(TRACK_X0, TRACK_X1 + 1):
            c.put(x, TRACK_Y, "·", DIM)
        if self.track:
            label, sub, style = self.track
            c.center(TRACK_Y - 1, label[:21], style, TRACK_X0, TRACK_X1 + 1)
            c.center(TRACK_Y + 1, sub[:21], DIM, TRACK_X0, TRACK_X1 + 1)

        x = 1
        for name, style in LEGEND:
            c.put(x, 10, "◓", style)
            c.put(x + 2, 10, name, DIM)
            x += len(name) + 6

        c.put(1, 11, "PARTY", TEXT)
        c.put(7, 11, "shared buffers · ♥ usage count (max 5)", DIM)
        draw_slots(c, self.slots, blink, SLOT_Y, SLOT_STYLE, SLOT_TAG, self.frame)

        shift = self.frame // 2 if self.rustle > 0 else 0
        self.rustle = max(0, self.rustle - 1)
        for x in range(1, self.W - 1):
            k = x + shift
            c.put(x, GRASS_Y, GRASS_CHARS[k % len(GRASS_CHARS)], GRASS if k % 3 else GRASS_DARK)
        c.center(GRASS_Y, " TALL GRASS · data files on disk ", TEXT)
        c.paged(3, GRASS_Y + 1, 94, 1, [(disk_line(self.slots, self.t), DIM)], self.frame, DIM)

        c.put(1, SAVE_Y, "SAVE", WAL)
        x = 7
        for i, rec in enumerate(self.tape):
            cell = f"[{rec['text']}]"
            if x + len(cell) > 76:
                c.put(x, SAVE_Y, f"+{len(self.tape) - i} more", DIM)
                break
            c.put(x, SAVE_Y, cell, DISK if rec["saved"] else WAL)
            x += len(cell)
        if self.tape:
            saved = all(r["saved"] for r in self.tape)
            c.put(88, SAVE_Y, "SAVED" if saved else "NOT SAVED", DISK if saved else WAL)

        c.box(0, TEXT_Y, self.W, 5, FRAME)
        c.put(self.W - len(KEYS_HINT) - 2, TEXT_Y + 4, KEYS_HINT, DIM)
        done = self.typed >= len(self.fun)
        text = [(self.fun[: self.typed], TEXT)] + ([(self.plain, EXPLAIN)] if done and self.plain else [])
        since = max(0, (self.typed - len(self.fun)) // TYPE_SPEED)
        c.paged(2, TEXT_Y + 1, 94, 3, text, since, DIM)
        if done and blink:
            c.put(97, TEXT_Y + 3, "▼", GOLD)

        for bx, by, glyph, style in self.balls:
            c.put(bx, by, glyph, style)

        if self.overlay:
            h = len(self.overlay) + 4
            w = max(_width(t) for t, _ in self.overlay) + 8
            x0, y0 = (self.W - w) // 2, (self.H - h) // 2
            c.box(x0, y0, w, h, GOLD)
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
        for _ in range(frames):
            if self.skip:
                break
            yield self.draw()
        self.skip = False

    def read_time(self, extra):
        """Frames to type the text box out, then to read it: longer text, longer pause."""
        words = len(f"{self.fun} {self.plain}".split())
        typing = -(-len(self.fun) // TYPE_SPEED)
        reading = round((extra + READ_FRAMES_PER_WORD * words) * self.reading)
        # text longer than the box is paged: leave every page its time on screen
        lines = len(wrap_items([(self.fun, ""), (self.plain, "")], 94))
        pages = -(-lines // 3)
        return typing + (max(reading, pages * PAGE_FRAMES) if pages > 1 else reading)

    def say(self, fun, plain=""):
        self.fun, self.plain, self.typed = fun, plain, 0

    def tell(self, fun, plain="", extra=14):
        """Say something and wait until it has been typed out and read."""
        self.say(fun, plain)
        yield from self.hold(self.read_time(extra))

    # ---- moves -----------------------------------------------------------
    def fly(self, to_backend, label, sub, style):
        self.track = (label, sub, style)
        xs = range(TRACK_X0, TRACK_X1 + 1) if to_backend else range(TRACK_X1, TRACK_X0 - 1, -1)
        for x in xs:
            self.balls = [(x, TRACK_Y, SPIN[x % 4], style)]
            yield self.draw()
            yield self.draw()               # two frames a cell, so the eye can follow it
        yield from self.land(xs[-1], style)
        yield from self.hold(6)
        self.track = None

    def land(self, x, style):
        """The ball bounces where it arrives, then flashes on impact."""
        for dy, glyph in ((-1, "◓"), (-2, "◓"), (-1, "◓"), (0, "◓"), (0, "✦"), (0, "✧"), (0, "·")):
            self.balls = [(x, TRACK_Y + dy, glyph, style if glyph == "◓" else GOLD)]
            yield self.draw()
            yield self.draw()
        self.balls = []

    def stage_x(self, name):
        return BACKEND_X + 2 + STAGE_ORDER.index(name) * 11 + 5

    def hop(self, name):
        """The query ball moves on to the stage now running."""
        target = self.stage_x(name)
        start = BACKEND_X + 1 if self.qball is None else self.qball
        for x in range(start, target + 1) if target >= start else [target]:
            self.qball = x
            yield self.draw()
            yield self.draw()

    def reply(self, kind, words):
        """A message the server sends back before the result, e.g. ParseComplete."""
        size = next((m["bytes"] for m in self.t["protocol"]["messages"] if m["type"] == kind), None)
        if size is None:
            return
        self.say(f"The server threw back {kind}!", words)
        yield from self.fly(False, kind, f"{size} B", QUERY)
        yield from self.hold(self.read_time(0))

    def slot_x(self, index):
        return SLOT_X0 + min(index, MAX_SLOTS - 1) * SLOT_W + SLOT_W // 2

    def tape_x(self):
        return min(7 + sum(len(r["text"]) + 2 for r in self.tape), 84)

    def throw(self, index):
        """Throw a ball into the tall grass, wobble, catch, bring it up."""
        self.throwing = True
        x0, y0, x1, y1 = 11, 4, self.slot_x(index), GRASS_Y
        steps = 20
        for s in range(steps + 1):
            f = s / steps
            x = round(x0 + (x1 - x0) * f)
            y = round(y0 + (y1 - y0) * f - 6 * math.sin(math.pi * f))
            self.balls = [(x, max(2, y), SPIN[s % 4], READ)]
            if s == 4:
                self.throwing = False
            yield self.draw()
        self.rustle = 14
        for dx in (0, -1, 0, 1, 0, -1, 0):
            self.balls = [(x1 + dx, GRASS_Y, "◓", READ)]
            yield from self.hold(3)
        self.balls = [(x1, GRASS_Y, "✦", GOLD)]
        yield from self.hold(5)
        for y in range(GRASS_Y - 1, SLOT_Y + SLOT_H - 1, -1):
            self.balls = [(x1, y, "◓", READ)]
            yield from self.hold(2)
        self.balls = []

    def pop(self, index, style):
        x = self.slot_x(index)
        for glyph in ("◓", "◓", "✦"):
            self.balls = [(x, SLOT_Y + SLOT_H, glyph, style)]
            yield from self.hold(3)
        self.balls = []

    def drop(self, from_x, from_y, to_x, to_y, style):
        steps = 10
        for s in range(steps + 1):
            f = s / steps
            self.balls = [(round(from_x + (to_x - from_x) * f),
                           round(from_y + (to_y - from_y) * f), SPIN[s % 4], style)]
            yield self.draw()
        self.balls = []

    # ---- the story -------------------------------------------------------
    def run(self):
        t = self.t
        result, proto, end = t["result"], t["protocol"], t["end"]

        self.overlay = [("PGTRAIL", CAP), ("", ""), ("a database adventure", TEXT),
                        ("", ""), ("one statement, every layer", DIM)]
        yield from self.hold(40)
        self.overlay = None

        label, size, words = request(t)
        self.say(f"You threw a {label} ball at the server! ({size} bytes)",
                 f"the client sends {words}")
        yield from self.fly(True, label, f"{size} B", QUERY)
        yield from self.hold(self.read_time(0))

        for name in STAGE_ORDER:
            if name not in self.stages:
                continue
            stage = self.stages[name]
            ms = stage.get("elapsed_ms")
            took = (f" · {ms:.3f} ms" if ms is not None else "") + (
                f" · {stage_cost(stage)}" if stage_cost(stage) else "")
            self.stage_state[name] = "active"
            yield from self.hop(name)
            if name == (wait_view(self.t) or {}).get("stage"):
                yield from self.wait_scene()
            if name == "execute":
                yield from self.tell("The server used EXECUTE!", stage["what"] + took, 10)
                yield from self.tree_scene("btree")
                yield from self.execute()
            else:
                yield from self.tell(f"The server used {name.upper()}!", stage["what"] + took)
            if name == "analyze":
                yield from self.tree_scene("query")
            if name == "plan":
                yield from self.plan_scene()
                yield from self.tree_scene("plan")
            self.stage_state[name] = "done"
            self.time_ms += ms or 0
            yield from self.hold(4)
            # the extended protocol answers each step as soon as it is done
            if name == "rewrite":
                yield from self.reply("ParseComplete", "extended protocol: the statement is parsed, "
                                      "analysed and rewritten, and the server says so right away")
            if name == "plan":
                yield from self.reply("BindComplete", "extended protocol: the parameters are bound and "
                                      "the plan is ready; the result comes next")
        self.qball = None                   # the result goes back instead

        if result["error"]:
            self.say("It failed! " + result["error"].splitlines()[0],
                     "the server answers with an ErrorResponse")
            yield from self.fly(False, "ERROR", "", DIRTY)
        else:
            rows = f"{result['row_count']} row(s)" if result["columns"] else ""
            self.say(f"The server threw back {result['status']}!",
                     f"the result goes back to the client {rows}".rstrip())
            yield from self.fly(False, (result["status"] or "DONE").upper(), rows, QUERY)
        self.time_ms = proto["round_trip_ms"]
        yield from self.hold(self.read_time(0))

        yield from self.finish(end)
        yield from self.aftermath()

        b = t.get("buffers", {}).get("summary", {})
        self.say("Thanks for playing!", FULL_REPORT_HINT)
        self.overlay = [
            ("TRAINER CARD" if not result["error"] else "STATEMENT FAILED",
             GOLD if not result["error"] else DIRTY),
            ("", ""),
            (f"ROUND TRIP        {proto['round_trip_ms']:9.3f} ms", TEXT),
            (f"{end['command']:<10}        {end['round_trip_ms']:9.3f} ms", TEXT),
            (f"CAUGHT (disk reads)      {b.get('loaded', 0):5d}", READ),
            (f"PAGES MODIFIED           {b.get('modified', 0):5d}", DIRTY),
            (f"SAVE FILE (WAL)       {self.wal_bytes:6d} B", WAL),
            (f"BADGES (locks)           {self.badges_earned:5d}", GOLD),
            (f"CACHE HITS (no catch)    {b.get('touched', 0) - b.get('loaded', 0):5d}", HIT),
        ]
        yield from self.hold(75)

    def wait_scene(self):
        """Another session at the same time: a wait, no wait, or a deadlock."""
        v = wait_view(self.t)
        if not v:
            return
        first, after_ok, after_bad, free = (
            {"blocked": "A rival trainer got there first and blocks your path!",
             "deadlock": "Both trainers are stuck, each waiting for the other!"},
            {"blocked": "The rival left after {ms:.1f} ms. Your turn!",
             "deadlock": "The referee sent the rival home after {ms:.0f} ms. Your turn!"},
            {"blocked": "The rival left after {ms:.1f} ms. Your turn!",
             "deadlock": "The referee sent YOU home after {ms:.0f} ms: your statement was cancelled!"},
            "A rival trainer was in the grass too, but didn't block you!")
        if not v["blocked"]:
            self.overlay = [(v["title"], HIT), ("", "")] + [(line, TEXT) for line in v["box"]]
            yield from self.tell(free, v["words"])
            self.overlay = None
            return
        self.say(first[v["kind"]], v["words"])
        frames = 90
        for i in range(1, frames + 1):
            self.overlay = ([(v["title"], DIRTY), ("", "")] + [(line, TEXT) for line in v["box"]]
                            + [("", ""), (f"waiting {v['waited'] * i / frames:8.1f} ms", GOLD),
                               ("█" * (i // 3) + "·" * (30 - i // 3), GOLD)])
            yield self.draw()
        yield from self.hold(max(0, self.read_time(0) - frames))
        mine = (self.t["lock_wait"].get("deadlock") or {}).get("victim") == "this statement"
        self.overlay[0] = ("DEADLOCK BROKEN" if v["kind"] == "deadlock" else "THE WAY IS CLEAR", HIT)
        end = (after_bad if mine else after_ok)[v["kind"]].format(ms=v["waited"])
        yield from self.tell(end, "the other session ended its transaction and its locks were "
                                  "released; the statement took its own lock and carried on"
                             if v["kind"] != "deadlock" else
                             "PostgreSQL's deadlock detector checks after deadlock_timeout and cancels "
                             "one of the two, so the other can go on")
        self.overlay = None

    def tree_scene(self, kind):
        """The server's own tree, a page at a time. summary shows no trees;
        detailed shows only the B-tree dive."""
        if self.detail == "summary" or (self.detail == "detailed" and kind != "btree"):
            return
        pages = tree_pages(self.t, kind, 80, 13)
        if kind == "btree":
            pages = [[[(text, HIT if on else TEXT) for text, on in parts] for parts in page]
                     for page in btree_pages(self.t, 80, 13)]   # the box must not cover the text box
        if kind == "btree" and no_index_words(self.t):
            yield from self.tell("No B-tree dive this time!", no_index_words(self.t).split(": ", 1)[1])
        if not pages or not pages[0]:
            return
        nodes = len(tree_rows(self.t, kind))    # the steps shown, not the server's full count
        width = max(_width(line) for page in pages for line in page)
        for n, page in enumerate(pages, 1):
            where = f" · page {n}/{len(pages)}" if len(pages) > 1 else ""
            self.overlay = [(TREE_TITLES[kind].upper(), GOLD), (f"{nodes} {'steps' if kind == 'btree' else 'nodes'}{where}", DIM),
                            ("", "")] + [(line if isinstance(line, list) else line.ljust(width), TEXT) for line in page]
            fun = ("The index scan dives down the B-tree!" if kind == "btree"
                   else f"The server built a {kind.upper()} TREE: {nodes} nodes!")
            yield from self.tell(fun, TREE_WORDS[kind] + ("" if kind == "btree"
                                                          else "; every node and field is in --json"))
        self.overlay = None

    def plan_scene(self):
        """A battle menu: the cursor visits each plan, then the cheapest is chosen."""
        if self.detail == "summary":
            return
        plans = considered(self.t)
        if len(plans) < 2:
            return
        table = plan_table(plans, 64, upper=True)
        names = [p["method"].upper() for p in plans]

        def menu(cursor, picked=False):
            rows = [("CHOOSE A PLAN", GOLD), ("estimated cost", DIM), ("", "")]
            for text, i, first in table:
                pointer = "▶" if i == cursor and first else " "
                style = (HIT if picked and i == 0 else GOLD if i == cursor else
                         DIM if picked else TEXT)
                rows.append((f"{pointer} {text}", style))
            return rows

        yield from self.tell(f"The planner found {len(plans)} ways to run this!",
                             "it estimates the cost of each and keeps only the cheapest")
        for i in list(range(1, len(plans))) + [0]:
            self.overlay = menu(i)
            p = plans[i]
            ratio = "" if p["chosen"] else f", {p['ratio']:.2f}× the cheapest"
            yield from self.tell(f"Option {i + 1}: {names[i]}? Cost {p['total_cost']:,.2f}{ratio}.",
                                 p["summary"])
        self.overlay = menu(0, picked=True)
        worst = plans[-1]
        fun = f"The server chose {names[0]}!"
        if worst["ratio"] >= 2:
            fun += f" It's super effective: {worst['ratio']:.1f}× cheaper than {names[-1]}."
        yield from self.tell(fun, "why: " + why_chosen(self.t), 20)
        self.overlay = None

    def execute(self):
        t = self.t
        for lock in t["locks"] if isinstance(t["locks"], list) else []:
            if lock["locktype"] != "relation":
                continue
            mode = lock["mode"].replace("Lock", "")
            self.badges.append((lock["target"], mode))
            self.badges_earned += 1
            yield from self.tell(f"You earned the {mode} badge for {lock['target']}!",
                                 f"lock manager: {lock['mode']}, {lock['why']}")

        groups = t["buffers"].get("relations", []) if isinstance(t["buffers"], dict) else []
        pages = [(g, p) for g in groups for p in g["pages"]]
        # indexes are read before the heap pages they point to
        pages.sort(key=lambda gp: (gp[0]["system"], gp[0]["kind"] != "index"))
        for g, p in pages:
            slot = new_slot(g, p)
            who = f"{slot['rel']} block {p['block']}"
            index = len(self.slots)
            if index >= MAX_SLOTS:
                self.slots.append(slot)
                continue
            via = "B-tree index" if g["kind"] == "index" else "table"
            if p["loaded"]:
                self.say(f"A wild {who} appeared in the tall grass!",
                         f"buffer manager: a miss, the {via} page is not in shared buffers yet")
                yield from self.hold(self.read_time(0))
                yield from self.throw(index)
                slot.update(state="read", flash=8)
                self.slots.append(slot)
                yield from self.tell(f"Gotcha! {who} joined your party!",
                                     f"buffer manager: read from {slot['file'] or 'the OS'} into "
                                     "a free slot of shared buffers")
            else:
                slot["flash"] = 8
                self.slots.append(slot)
                self.say(f"{who} is already in your party. Go!",
                         f"buffer manager: a hit for the {via} page, no disk read; usage count "
                         f"{p['usage_before']}→{p['usage_after']}")
                yield from self.pop(index, HIT)
                yield from self.hold(self.read_time(6))
            if p["modified"]:
                slot.update(state="dirty", flash=8)
                yield from self.tell(f"{who} is now DRT!",
                                     "modified in memory only, its data file is written later. MVCC: "
                                     "other sessions don't see the change until it commits")
            elif p["dirtied"]:
                slot.update(state="soft", flash=8)
                yield from self.tell(f"{who} is now DRT*!",
                                     "dirtied without a WAL record of its own (e.g. hint bits)")
        if len(pages) > MAX_SLOTS:
            yield from self.tell(f"...and {len(pages) - MAX_SLOTS} more joined the party",
                                 more_pages(pages[MAX_SLOTS:]), 20)

        wal = t["wal"].get("records", []) if isinstance(t["wal"], dict) else []
        wal = [r for r in wal if r["resource_manager"] != "Transaction" and (
            r["owner"] == "statement" or r["blocks"])]
        for rec in wal[:MAX_ANIMATED_WAL]:
            index = self._slot_of(rec)
            start = (self.slot_x(index), SLOT_Y + SLOT_H) if index is not None else (60, 9)
            self.say(f"{rec['record_type']} was written to the save log! ({rec['bytes']} B)",
                     f"write-ahead log: record {rec['resource_manager']}/{rec['record_type']}, in memory, "
                     "not on disk yet")
            yield from self.drop(*start, self.tape_x(), SAVE_Y, WAL)
            self.tape.append({"text": f"{rec['record_type']} {rec['bytes']}B", "saved": False})
            self.wal_bytes += rec["bytes"]
            yield from self.hold(self.read_time(8))
            if rec["fpi_bytes"]:
                yield from self.tell(f"It's a full-page image! +{rec['fpi_bytes']} B",
                                     "first change to this page since the last checkpoint, so the "
                                     "whole page is logged to survive a torn write")
        rest = wal[MAX_ANIMATED_WAL:]
        if rest:
            for rec in rest:
                self.tape.append({"text": f"{rec['record_type']} {rec['bytes']}B", "saved": False})
                self.wal_bytes += rec["bytes"]
            yield from self.tell(f"...and {len(rest)} more records went into the save log",
                                 f"{sum(r['bytes'] for r in rest)} more WAL bytes", 20)

    def _slot_of(self, rec):
        for b in rec["blocks"]:
            name = b["relation"] + ("" if b["fork"] == "main" else f"[{b['fork']}]")
            for i, s in enumerate(self.slots):
                if s["rel"] == name and s["block"] == b["block"]:
                    return i
        return None

    def finish(self, end):
        t = self.t
        cmd = end["command"]
        self.say(f"You threw a {cmd} ball!", f"the client sends {cmd}")
        yield from self.fly(True, cmd, "", QUERY)
        yield from self.hold(self.read_time(0))
        wal = t["wal"].get("records", []) if isinstance(t["wal"], dict) else []
        for rec in wal:
            if rec["resource_manager"] == "Transaction" and rec["owner"] == "statement":
                yield from self.drop(60, 9, self.tape_x(), SAVE_Y, WAL)
                self.tape.append({"text": f"{rec['record_type']} {rec['bytes']}B", "saved": False})
                self.wal_bytes += rec["bytes"]
                yield from self.hold(8)
        if cmd == "COMMIT" and self.tape:
            self.say("SAVING... Don't turn off the power.",
                     "transaction manager: COMMIT waits while the WAL is flushed to disk (fsync)")
            for i in range(max(30, self.read_time(0))):
                self.overlay = [(f"SAVING{'.' * (i // 5 % 4):<3}", GOLD), ("", ""),
                                ("Don't turn off the power.", TEXT)]
                yield self.draw()
            self.overlay = None
            for rec in self.tape:
                rec["saved"] = True
            yield from self.tell("Game saved!",
                                 "the WAL is on disk, so the change survives a crash, and the "
                                 "transaction is marked committed in pg_xact: everyone sees it now. "
                                 + wal_files(t))
        elif cmd == "ROLLBACK":
            yield from self.tell("Got away safely!",
                                 "transaction manager: ROLLBACK marks the transaction aborted in "
                                 "pg_xact; its row versions stay, but nobody will ever see them")
        self.say(f"{cmd} confirmed after {end['round_trip_ms']:.3f} ms",
                 "transaction over: locks released, so the badges go back")
        yield from self.fly(False, cmd, f"{end['round_trip_ms']:.3f} ms", QUERY)
        self.badges = []
        yield from self.hold(self.read_time(0))

    def aftermath(self):
        a = self.t.get("aftermath")
        dirty = [(i, s) for i, s in enumerate(self.slots) if s["state"] in ("dirty", "soft")]
        if not a or not dirty:
            return
        for _, s in dirty:
            s["alarm"] = True
        yield from self.tell(f"{len(dirty)} party member(s) still have status DRT!",
                             "dirty pages: the change is in memory and in the WAL, not in the "
                             "data files", 30)
        if "checkpoint_ms" in a:
            yield from self.tell("Off to the healing center! (CHECKPOINT)",
                                 "the checkpointer writes every dirty page to its data file")
            for i, s in dirty:
                self.say(f"{s['rel']} block {s['block']} went back to the tall grass",
                         f"CHECKPOINT writes {s['rel']} block {s['block']} to "
                         f"{s.get('file') or 'its data file'}")
                yield from self.drop(self.slot_x(i), SLOT_Y + SLOT_H, self.slot_x(i), GRASS_Y, DISK)
                self.rustle = 8
                s.update(state="clean", alarm=False, flash=6)
                yield from self.hold(self.read_time(6))
            yield from self.tell("Your party is fully healed!",
                                 f"CHECKPOINT done in {a['checkpoint_ms']:.1f} ms: data files and "
                                 "WAL agree", 24)
        else:
            yield from self.tell("Heal them later at a checkpoint.",
                                 "the checkpointer writes them eventually (try --checkpoint)", 24)



def wal_files(trace):
    files = trace["wal"].get("files", []) if isinstance(trace["wal"], dict) else []
    return ", ".join(files) or "the WAL"


def disk_line(slots, trace):
    """The data files behind the pages so far, relative to the data directory."""
    files = []
    for s in slots:
        entry = f"{relative(s['file'], trace)} {s['rel']}" if s.get("file") else s["rel"]
        if entry not in files:
            files.append(entry)
    return " · ".join(files)


def new_slot(g, p):
    name = g["name"] + ("" if g["fork"] == "main" else f"[{g['fork']}]")
    return {"rel": name, "base": g["name"], "fork": g["fork"], "block": p["block"],
            "file": p.get("file") or g.get("file"),
            "state": "hit", "flash": 0, "usage": p["usage_after"], "usage_before": p["usage_before"]}


def more_pages(pages):
    read = sum(p["loaded"] for _, p in pages)
    modified = sum(p["modified"] for _, p in pages)
    return (f"{read} read from disk, {len(pages) - read} cache hits, {modified} modified "
            "(only the first 8 buffers are drawn)")


def draw_slots(c, slots, blink, y, styles, tags, clock=0, x0=SLOT_X0, w=SLOT_W, h=SLOT_H):
    """shared_buffers slots: name, fork and block, usage count as hearts, state tag."""
    for i, slot in enumerate(slots[:MAX_SLOTS]):
        x = x0 + i * w
        style = styles[slot["state"]]
        if slot.get("flash", 0) > 0:
            slot["flash"] -= 1
            style = TEXT if blink else style
        if slot["state"] in ("dirty", "soft") and slot.get("alarm") and not blink:
            style = DIM
        c.box(x, y, w, h, style)
        first, second = name_lines(slot["base"], w - 2, clock)
        c.put(x + 1, y + 1, first, style)
        c.put(x + 1, y + 2, second, style)
        where = f"block {slot['block']}" if slot["fork"] == "main" else f"{slot['fork']} blk {slot['block']}"
        c.put(x + 1, y + 3, where[: w - 2], style)
        usage = min(slot["usage"] or 0, 5)
        c.center(y + 4, "♥" * usage + "♡" * (5 - usage), style, x, x + w)
        before = slot.get("usage_before")
        c.center(y + 5, "new" if before is None else f"{before}→{slot['usage']}", DIM, x, x + w)
        c.center(y + h - 1, f" {tags[slot['state']]} ", style, x, x + w)
    if len(slots) > MAX_SLOTS:
        c.put(x0 + MAX_SLOTS * w + 1, y + 3, f"+{len(slots) - MAX_SLOTS}", DIM)
