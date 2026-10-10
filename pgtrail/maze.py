"""Maze theme: the arcade replay as a maze chase.

The trace becomes a row of pellets through a three-lane maze, one lane per
phase (wire and planning, execution, commit), eaten in the order things
happened. Pellet colors are the same event colors as every other theme.
Locks are ghosts that chase the chomper while they are held; the COMMIT fsync
is the power pellet that sends them home.
"""
from .arcade import (_width, FULL_REPORT_HINT, KEYS_HINT, MAX_SLOTS, PAGE_FRAMES, STAGE_ORDER, sql_canvas,
                     wrap_items)
from .catch import (MAX_ANIMATED_WAL, READ_FRAMES_PER_WORD, TYPE_SPEED, disk_line, wal_files,
                    draw_slots, more_pages, new_slot)
from .render import (TREE_TITLES, TREE_WORDS, considered, lock_wait, plan_name, plan_table,
                     request, stage_cost, tree_pages, tree_rows, why_chosen, no_index_words, btree_pages, wait_view)
from .palette import BG, DIM, EXPLAIN, DIRTY, DISK, GOLD, HIT, QUERY, READ, TEXT, WAL, rgb

WALL = rgb(45, 80, 230, "1;34")
CHOMP = rgb(255, 220, 40, "1;33")
FRIGHT = rgb(70, 90, 255, "1;34")
GHOST_COLORS = (rgb(255, 70, 70, "1;31"), rgb(255, 150, 210, "1;35"),
                rgb(70, 225, 235, "1;36"), rgb(255, 170, 60, "1;33"))
LEGEND = (("•", QUERY, "message"), ("•", TEXT, "stage"), ("♦", HIT, "cache hit"),
          ("◆", READ, "disk read"), ("•", WAL, "WAL"), ("●", DISK, "to disk"),
          ("ᗣ", GHOST_COLORS[0], "lock"))
SLOT_STYLE = {"read": READ, "hit": HIT, "dirty": DIRTY, "soft": DIRTY, "clean": DISK}
SLOT_TAG = {"read": "READ IN", "hit": "HIT", "dirty": "DIRTY", "soft": "DIRTY*", "clean": "CLEAN"}

# layout of the 100x30 screen
MAZE_X0, MAZE_X1 = 1, 62               # outer walls
LANE_X0, LANE_X1 = 3, 60               # where pellets go
LANE_Y = (3, 5, 7)
LANE_LABELS = (" 1 WIRE · PARSE · PLAN ", " 2 EXECUTE: locks, pages, WAL ",
               " 3 COMMIT · CHECKPOINT ")
SLOT_X0, SLOT_W, SLOT_Y, SLOT_H = 2, 12, 12, 7
DISK_Y, WAL_Y, TEXT_Y = 20, 23, 25
HOUSE_X = 64


def _path():
    """Lane 1 left to right, down, lane 2 back, down, lane 3 left to right."""
    p = [(x, LANE_Y[0]) for x in range(LANE_X0, LANE_X1 + 1)]
    p.append((LANE_X1, LANE_Y[0] + 1))
    p += [(x, LANE_Y[1]) for x in range(LANE_X1, LANE_X0 - 1, -1)]
    p.append((LANE_X0, LANE_Y[1] + 1))
    p += [(x, LANE_Y[2]) for x in range(LANE_X0, LANE_X1 + 1)]
    return p


PATH = _path()
LANE_LEN = LANE_X1 - LANE_X0 + 1
LANE_START = (0, LANE_LEN + 1, 2 * LANE_LEN + 2)


class MazeGame:
    W, H = 100, 30
    name = "maze"

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
        self.stage_state = {}
        self.ghosts = []               # {target, mode, style, state: chase|fright|home}
        self.slots = []
        self.tape = []                 # {text, saved}
        self.sprites = []              # (x, y, glyph, style)
        self.pellets = {}              # path index → (glyph, style)
        self.pos = 0
        self.facing = 1
        self.fright = 0
        self.fright_back = 0           # how far frightened ghosts have run back
        self.fun = self.plain = ""
        self.typed = 0
        self.overlay = None
        self.disk_flash = 0
        self.reads = self.hits = 0

    # ---- drawing ---------------------------------------------------------
    def draw(self):
        c = sql_canvas(self.t, self.W, self.H, BG, self.extra_rows, self.frame, DIM)
        self.frame += 1
        self.typed += TYPE_SPEED
        blink = (self.frame // 5) % 2 == 0

        held = sum(g["state"] == "chase" for g in self.ghosts)
        c.put(1, 0, "PGTRAIL", CHOMP)
        c.put(11, 0, f"TIME {self.time_ms:8.3f} ms", TEXT)
        c.put(33, 0, f"PAGES {len(self.slots):02d}", HIT)
        c.put(46, 0, f"WAL {self.wal_bytes:06d} B", WAL)
        c.put(64, 0, f"GHOSTS {held:02d}", GHOST_COLORS[0])

        self._maze(c)
        for i, (glyph, style) in self.pellets.items():
            x, y = PATH[i]
            c.put(x, y, glyph, style)
        for i, g in enumerate(g for g in self.ghosts if g["state"] in ("chase", "fright")):
            at = self.pos - 4 * (i + 1) - 1 - (self.fright_back if g["state"] == "fright" else 0)
            if at >= 0:
                x, y = PATH[at]
                style = (TEXT if blink and self.fright < 20 else FRIGHT) if g["state"] == "fright" else g["style"]
                c.put(x, y, "ᗣ", style)
        x, y = PATH[self.pos]
        mouth = (self.frame // 3) % 2 == 0
        c.put(x, y, ("ᗧ" if self.facing > 0 else "ᗤ") if mouth else "●", CHOMP)

        c.box(HOUSE_X, 2, self.W - HOUSE_X - 1, 7, WALL, "GHOST HOUSE · locks")
        house = [(f"ᗣ {g['target']} {g['mode']}",
                  g["style"] if g["state"] == "chase" else FRIGHT if g["state"] == "fright" else DIM)
                 for g in self.ghosts]
        if house:
            c.paged(HOUSE_X + 1, 3, self.W - HOUSE_X - 3, 5, house, self.frame, DIM)
        if not self.ghosts:
            c.center(5, "no locks taken yet", DIM, HOUSE_X, self.W - 1)

        x = 1
        for name in STAGE_ORDER:
            state = self.stage_state.get(name)
            ms = self.stages.get(name, {}).get("elapsed_ms")
            text = name.upper() + (f" {ms:.3f}" if state == "done" and ms is not None else "")
            style = DIM if state is None else (GOLD if state == "active" and blink else
                                               TEXT if state == "active" else HIT)
            c.put(x, 9, text, style)
            x += len(text) + 3
        x = 1
        for glyph, style, name in LEGEND:
            c.put(x, 10, glyph, style)
            c.put(x + 2, 10, name, DIM)
            x += len(name) + 5

        c.put(1, 11, "SHARED BUFFERS", TEXT)
        c.put(16, 11, "in memory · ♥ usage count (max 5)", DIM)
        draw_slots(c, self.slots, blink, SLOT_Y, SLOT_STYLE, SLOT_TAG, self.frame)

        disk_style = TEXT if self.disk_flash > 0 and blink else WALL
        self.disk_flash = max(0, self.disk_flash - 1)
        c.box(1, DISK_Y, 98, 3, disk_style, "DISK · data files")
        c.paged(3, DISK_Y + 1, 94, 1, [(disk_line(self.slots, self.t), DIM)], self.frame, DIM)

        c.put(1, WAL_Y, "WAL", WAL)
        x = 6
        for i, rec in enumerate(self.tape):
            cell = f"[{rec['text']}]"
            if x + len(cell) > 76:
                c.put(x, WAL_Y, f"+{len(self.tape) - i} more", DIM)
                break
            c.put(x, WAL_Y, cell, DISK if rec["saved"] else WAL)
            x += len(cell)
        if self.tape:
            saved = all(r["saved"] for r in self.tape)
            c.put(88, WAL_Y, "ON DISK" if saved else "IN MEMORY", DISK if saved else WAL)

        c.box(0, TEXT_Y, self.W, 5, WALL)
        c.put(self.W - len(KEYS_HINT) - 2, TEXT_Y + 4, KEYS_HINT, DIM)
        done = self.typed >= len(self.fun)
        text = [(self.fun[: self.typed], TEXT)] + ([(self.plain, EXPLAIN)] if done and self.plain else [])
        since = max(0, (self.typed - len(self.fun)) // TYPE_SPEED)
        c.paged(2, TEXT_Y + 1, 94, 3, text, since, DIM)
        if done and blink:
            c.put(97, TEXT_Y + 3, "▼", GOLD)

        for sx, sy, glyph, style in self.sprites:
            c.put(sx, sy, glyph, style)

        if self.overlay:
            h = len(self.overlay) + 4
            w = max(_width(t) for t, _ in self.overlay) + 8
            x0, y0 = (self.W - w) // 2, (self.H - h) // 2
            c.box(x0, y0, w, h, CHOMP)
            for i, (text, style) in enumerate(self.overlay):
                if isinstance(text, list):      # a line in colored pieces: (text, style)
                    x = x0 + (w - _width(text)) // 2
                    for piece, piece_style in text:
                        c.put(x, y0 + 2 + i, piece, piece_style or style)
                        x += len(piece)
                else:
                    c.center(y0 + 2 + i, text, style, x0, x0 + w)
        return c

    def _maze(self, c):
        top, mid1, mid2, bottom = LANE_Y[0] - 1, LANE_Y[0] + 1, LANE_Y[1] + 1, LANE_Y[2] + 1
        width = MAZE_X1 - MAZE_X0 - 1
        c.put(MAZE_X0, top, "╔" + "═" * width + "╗", WALL)
        c.put(MAZE_X0, bottom, "╚" + "═" * width + "╝", WALL)
        for y in LANE_Y:
            c.put(MAZE_X0, y, "║", WALL)
            c.put(MAZE_X1, y, "║", WALL)
        # the gaps where the lanes connect: right end of wall 1, left end of wall 2
        c.put(MAZE_X0, mid1, "╠" + "═" * (LANE_X1 - MAZE_X0 - 1), WALL)
        c.put(MAZE_X1, mid1, "║", WALL)
        c.put(MAZE_X0, mid2, "║", WALL)
        c.put(LANE_X0 + 1, mid2, "═" * (MAZE_X1 - LANE_X0 - 1) + "╣", WALL)
        for i, (y, label) in enumerate(zip((top, mid1, mid2), LANE_LABELS)):
            lane = self.lane_of(self.pos)
            c.put(6, y, label, GOLD if lane == i else DIM)

    @staticmethod
    def lane_of(index):
        return 0 if index < LANE_START[1] else 1 if index < LANE_START[2] else 2

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
        self.say(fun, plain)
        yield from self.hold(self.read_time(extra))

    # ---- moves -----------------------------------------------------------
    def walk_to(self, target):
        while self.pos < target:
            self.pos += 1
            if PATH[self.pos][1] == PATH[self.pos - 1][1]:
                self.facing = 1 if PATH[self.pos][0] > PATH[self.pos - 1][0] else -1
            yield self.draw()

    def slot_x(self, index):
        return SLOT_X0 + min(index, MAX_SLOTS - 1) * SLOT_W + SLOT_W // 2

    def tape_x(self):
        return min(6 + sum(len(r["text"]) + 2 for r in self.tape), 84)

    def glide(self, from_x, from_y, to_x, to_y, glyph, style, steps=10):
        for s in range(steps + 1):
            f = s / steps
            self.sprites = [(round(from_x + (to_x - from_x) * f),
                             round(from_y + (to_y - from_y) * f), glyph, style)]
            yield self.draw()
        self.sprites = []

    # ---- building the course ---------------------------------------------
    def events(self):
        """(lane, glyph, style, effect) for everything the trace measured, in order."""
        t = self.t
        lanes = ([], [], [])
        lanes[0].append(("•", QUERY, self.e_query))
        for name in STAGE_ORDER[:-1]:
            if name in self.stages:
                lanes[0].append(("•", TEXT, lambda n=name: self.e_stage(n)))

        # another session, when the wait (or no wait) happens while executing
        if (wait_view(t) or {}).get("stage") == "execute":
            lanes[1].append(("ᗣ", GHOST_COLORS[0], self.wait_scene))
        # how the executor reaches the rows: down a B-tree, or by reading tables in full
        if (tree_rows(t, "btree") or no_index_words(t)) and self.detail != "summary":
            lanes[1].append(("♦", HIT, lambda: self.tree_scene("btree")))
        for lock in t["locks"] if isinstance(t["locks"], list) else []:
            if lock["locktype"] == "relation":
                lanes[1].append(("•", GOLD, lambda lk=lock: self.e_lock(lk)))
        groups = t["buffers"].get("relations", []) if isinstance(t["buffers"], dict) else []
        pages = [(g, p) for g in groups for p in g["pages"]]
        pages.sort(key=lambda gp: (gp[0]["system"], gp[0]["kind"] != "index"))
        for g, p in pages[:MAX_SLOTS]:
            glyph, style = ("◆", READ) if p["loaded"] else ("♦", HIT)
            lanes[1].append((glyph, style, lambda g=g, p=p: self.e_page(g, p)))
        if len(pages) > MAX_SLOTS:
            lanes[1].append(("♦", HIT, lambda: self.e_more_pages(pages[MAX_SLOTS:])))
        wal = t["wal"].get("records", []) if isinstance(t["wal"], dict) else []
        changes = [r for r in wal if r["resource_manager"] != "Transaction" and (
            r["owner"] == "statement" or r["blocks"])]
        for rec in changes[:MAX_ANIMATED_WAL]:
            lanes[1].append(("•", WAL, lambda r=rec: self.e_wal(r)))
        if len(changes) > MAX_ANIMATED_WAL:
            lanes[1].append(("•", WAL, lambda: self.e_more_wal(changes[MAX_ANIMATED_WAL:])))

        end = t["end"]
        lanes[2].append(("•", DIRTY if t["result"]["error"] else QUERY, self.e_result))
        lanes[2].append(("•", QUERY, self.e_end_sent))
        for rec in wal:
            if rec["resource_manager"] == "Transaction" and rec["owner"] == "statement":
                lanes[2].append(("•", WAL, lambda r=rec: self.e_wal(r)))
        lanes[2].append(("●", DISK, self.e_power) if end["command"] == "COMMIT"
                        else ("●", DIM, self.e_power))
        lanes[2].append(("•", QUERY, self.e_confirmed))
        dirty = sum(p["modified"] or p["dirtied"] for _, p in pages)
        a = t.get("aftermath") or {}
        if dirty and "checkpoint_ms" in a:
            lanes[2].append(("●", DISK, self.e_checkpoint))
        elif dirty:
            lanes[2].append(("•", DIRTY, self.e_still_dirty))

        out = []
        for lane, items in enumerate(lanes):
            if len(items) > LANE_LEN // 2:
                items = items[: LANE_LEN // 2 - 1] + [("…", DIM, lambda n=len(items) - LANE_LEN // 2 + 1:
                                                       self.tell(f"…and {n} more", "see the full report"))]
            step = LANE_LEN / (len(items) + 1)
            for j, (glyph, style, effect) in enumerate(items):
                out.append((LANE_START[lane] + round((j + 1) * step), glyph, style, effect))
        return out

    # ---- effects ---------------------------------------------------------
    def e_query(self):
        label, size, words = request(self.t)
        yield from self.tell(f"CHOMP! {label} is on its way ({size} B)",
                             f"client → server: {words}")

    def e_stage(self, name):
        st = self.stages[name]
        self.stage_state[name] = "done"
        self.time_ms += st.get("elapsed_ms") or 0
        ms = st.get("elapsed_ms")
        yield from self.tell(f"{name.upper()} eaten" + (f" in {ms:.3f} ms" if ms is not None else ""),
                             st["what"] + (f" · {stage_cost(st)}" if stage_cost(st) else ""))
        if name == (wait_view(self.t) or {}).get("stage"):
            yield from self.wait_scene()
        if name == "analyze":
            yield from self.tree_scene("query")
        if name == "plan":
            yield from self.plan_scene()
            yield from self.tree_scene("plan")

    def wait_scene(self):
        """Another session at the same time: a wait, no wait, or a deadlock."""
        v = wait_view(self.t)
        if not v:
            return
        first, after_ok, after_bad, free = (
            {"blocked": "A ghost from another session blocks the lane!",
             "deadlock": "Two chompers, each waiting for the other!"},
            {"blocked": "The ghost left after {ms:.1f} ms",
             "deadlock": "Deadlock! The other chomper was removed after {ms:.0f} ms"},
            {"blocked": "The ghost left after {ms:.1f} ms",
             "deadlock": "Deadlock! YOUR chomper was removed after {ms:.0f} ms"},
            "Another session's ghost passed by without blocking you")
        if not v["blocked"]:
            self.overlay = [(v["title"], HIT), ("", "")] + [(line, TEXT) for line in v["box"]]
            yield from self.tell(free, v["words"])
            self.overlay = None
            return
        self.say(first[v["kind"]], v["words"])
        frames = 90
        for i in range(1, frames + 1):
            self.overlay = ([(v["title"], GHOST_COLORS[0]), ("", "")] + [(line, TEXT) for line in v["box"]]
                            + [("", ""), (f"waiting {v['waited'] * i / frames:8.1f} ms", CHOMP),
                               ("█" * (i // 3) + "·" * (30 - i // 3), CHOMP)])
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
            yield from self.tell("No B-tree dive this time", no_index_words(self.t).split(": ", 1)[1])
        if not pages or not pages[0]:
            return
        nodes = len(tree_rows(self.t, kind))    # the steps shown, not the server's full count
        width = max(_width(line) for page in pages for line in page)
        for n, page in enumerate(pages, 1):
            where = f" · page {n}/{len(pages)}" if len(pages) > 1 else ""
            self.overlay = [(TREE_TITLES[kind].upper(), CHOMP), (f"{nodes} {'steps' if kind == 'btree' else 'nodes'}{where}", DIM),
                            ("", "")] + [(line if isinstance(line, list) else line.ljust(width), TEXT) for line in page]
            fun = "Dive! The index scan walks down the B-tree" if kind == "btree" else f"{kind.capitalize()} tree: {nodes} nodes"
            yield from self.tell(fun, TREE_WORDS[kind] + ("" if kind == "btree"
                                                          else "; every node and field is in --json"))
        self.overlay = None

    def plan_scene(self):
        """The routes the planner compared; the cheapest one is taken."""
        if self.detail == "summary":
            return
        plans = considered(self.t)
        if len(plans) < 2:
            return
        table = plan_table(plans, 64)

        def board(lit, taken=False):
            rows = [("ROUTES THE PLANNER COMPARED", CHOMP), ("estimated cost", DIM), ("", "")]
            for text, i, first in table:
                if i >= lit:
                    break
                style = HIT if taken and i == 0 else DIM if taken else TEXT
                mark = ("ᗧ" if taken and i == 0 else "·") if first else " "
                rows.append((f"{mark} {text}", style))
            return rows

        for lit in range(1, len(plans) + 1):
            self.overlay = board(lit)
            p = plans[lit - 1]
            extra = "" if p["chosen"] else f", {p['ratio']:.2f}× the cheapest"
            yield from self.tell(f"Route {lit}: cost {p['total_cost']:,.2f}{extra}", p["summary"])
        self.overlay = board(len(plans), taken=True)
        yield from self.tell(f"The cheapest route wins: {plan_name(plans[0])}", "why: " + why_chosen(self.t), 20)
        self.overlay = None

    def e_lock(self, lock):
        mode = lock["mode"].replace("Lock", "")
        self.ghosts.append({"target": lock["target"], "mode": mode, "state": "chase",
                            "style": GHOST_COLORS[len(self.ghosts) % len(GHOST_COLORS)]})
        yield from self.tell(f"A ghost leaves the house: {mode} on {lock['target']}!",
                             f"lock manager: granted, and held until the transaction ends; {lock['why']}")

    def e_page(self, g, p):
        slot = new_slot(g, p)
        slot["flash"] = 8
        who = f"{slot['rel']} block {p['block']}"
        index = len(self.slots)
        if p["loaded"]:
            self.reads += 1
            self.say(f"Chewing... {who} is not in memory!",
                     "buffer manager: a miss, the backend waits while the page is read from the OS")
            self.disk_flash = 10
            yield from self.hold(self.read_time(0))
            sx = self.slot_x(index)
            yield from self.glide(sx, DISK_Y + 1, sx, SLOT_Y + SLOT_H, "◆", READ, 6)
            slot["state"] = "read"
            self.slots.append(slot)
            yield from self.tell(f"{who} read into shared buffers",
                                 f"read from {slot['file'] or 'the OS'}; a free buffer slot now "
                                 "holds a copy of the page", 8)
        else:
            self.hits += 1
            self.slots.append(slot)
            yield from self.tell(f"Cache hit! {who}",
                                 f"buffer manager: already in shared buffers, no disk read; usage count "
                                 f"{p['usage_before']}→{p['usage_after']}", 8)
        if p["modified"]:
            slot.update(state="dirty", flash=8)
            yield from self.tell(f"{who} is now DIRTY",
                                 "modified in memory only, its data file is written later. MVCC: other sessions "
                                 "don't see the change until it commits")
        elif p["dirtied"]:
            slot.update(state="soft", flash=8)
            yield from self.tell(f"{who} is now DIRTY*",
                                 "dirtied without a WAL record of its own (e.g. hint bits)")

    def e_wal(self, rec):
        index = None
        for b in rec["blocks"]:
            name = b["relation"] + ("" if b["fork"] == "main" else f"[{b['fork']}]")
            index = next((i for i, s in enumerate(self.slots)
                          if s["rel"] == name and s["block"] == b["block"]), index)
        x, y = PATH[self.pos]
        start = (self.slot_x(index), SLOT_Y + SLOT_H) if index is not None else (x, y + 1)
        fpi = f", {rec['fpi_bytes']} B of it a full-page image" if rec["fpi_bytes"] else ""
        self.say(f"WAL record {rec['resource_manager']}/{rec['record_type']} ({rec['bytes']} B)",
                 f"write-ahead log: appended to the WAL buffer in memory, not on disk yet{fpi}")
        yield from self.glide(*start, self.tape_x(), WAL_Y, "•", WAL)
        self.tape.append({"text": f"{rec['record_type']} {rec['bytes']}B", "saved": False})
        self.wal_bytes += rec["bytes"]
        yield from self.hold(self.read_time(10))

    def e_more_pages(self, pages):
        for g, p in pages:
            self.slots.append(new_slot(g, p))
            self.reads += p["loaded"]
            self.hits += not p["loaded"]
        yield from self.tell(f"...and {len(pages)} more pages", more_pages(pages), 20)

    def e_more_wal(self, records):
        for rec in records:
            self.tape.append({"text": f"{rec['record_type']} {rec['bytes']}B", "saved": False})
            self.wal_bytes += rec["bytes"]
        yield from self.tell(f"...and {len(records)} more WAL records",
                             f"{sum(r['bytes'] for r in records)} more WAL bytes", 20)

    def e_result(self):
        result, proto = self.t["result"], self.t["protocol"]
        if "execute" in self.stages:
            self.stage_state["execute"] = "done"
        self.time_ms = proto["round_trip_ms"]
        if result["error"]:
            yield from self.tell("Ouch! " + result["error"].splitlines()[0],
                                 "the server answers with an ErrorResponse")
        else:
            rows = f", {result['row_count']} row(s)" if result["columns"] else ""
            yield from self.tell(f"The answer goes back: {result['status']}{rows}",
                                 f"server → client, round trip {proto['round_trip_ms']:.3f} ms")

    def e_end_sent(self):
        cmd = self.t["end"]["command"]
        yield from self.tell(f"{cmd} sent", f"the client ends the transaction with {cmd}")

    def e_power(self):
        cmd = self.t["end"]["command"]
        chasing = [g for g in self.ghosts if g["state"] == "chase"]
        if cmd == "COMMIT" and self.tape:
            self.say("POWER PELLET! The WAL is flushed to disk (fsync)",
                     "transaction manager: COMMIT waits for this; with the WAL on disk the change survives a "
                     "crash, and pg_xact marks the transaction committed: "
                     + wal_files(self.t))
            self.disk_flash = 14
            for rec in self.tape:
                rec["saved"] = True
        else:
            self.say(f"{cmd}: nothing to wait for",
                     "transaction manager: no WAL needs to reach disk before the client gets its answer")
        yield from self.hold(self.read_time(0))
        for g in chasing:
            g["state"] = "fright"
        self.fright = 45
        for _ in range(45):
            self.fright -= 1
            self.fright_back = 45 - self.fright
            yield self.draw()
        for g in chasing:
            g["state"] = "home"
        self.fright_back = 0
        if chasing:
            yield from self.tell(f"{len(chasing)} ghost(s) went home",
                                 "the transaction is over, so its locks are released")

    def e_confirmed(self):
        end = self.t["end"]
        yield from self.tell(f"{end['command']} confirmed after {end['round_trip_ms']:.3f} ms",
                             "ReadyForQuery: the connection is idle again")

    def e_still_dirty(self):
        dirty = [s for s in self.slots if s["state"] in ("dirty", "soft")]
        for s in dirty:
            s["alarm"] = True
        yield from self.tell(f"{len(dirty)} page(s) are still DIRTY",
                             "the change is in memory and in the WAL, not in the data files; "
                             "the checkpointer writes them later (try --checkpoint)", 30)

    def e_checkpoint(self):
        a = self.t["aftermath"]
        yield from self.tell("CHECKPOINT! Dirty pages go back to disk",
                             "the checkpointer writes every dirty page to its data file")
        for i, s in enumerate(self.slots):
            if s["state"] not in ("dirty", "soft"):
                continue
            sx = self.slot_x(i)
            self.say(f"{s['rel']} block {s['block']} → data file",
                     f"the checkpointer writes it to {s.get('file') or 'its data file'}")
            yield from self.glide(sx, SLOT_Y + SLOT_H, sx, DISK_Y + 1, "●", DISK, 6)
            self.disk_flash = 6
            s.update(state="clean", flash=6)
            yield from self.hold(self.read_time(0))
        yield from self.tell(f"CHECKPOINT done in {a['checkpoint_ms']:.1f} ms",
                             "data files and WAL agree; older WAL can now be recycled", 20)

    # ---- the story -------------------------------------------------------
    def run(self):
        t = self.t
        course = self.events()
        self.pellets = {i: (glyph, style) for i, glyph, style, _ in course}
        self.overlay = [("PGTRAIL", CHOMP), ("", ""), ("eat the trace, pellet by pellet", TEXT),
                        ("", ""), ("READY!", GOLD)]
        yield from self.hold(40)
        self.overlay = None

        for index, _, _, effect in course:
            lane = self.lane_of(index)
            if lane >= 1 and "execute" in self.stages and "execute" not in self.stage_state:
                self.stage_state["execute"] = "active"
            yield from self.walk_to(index)
            self.pellets.pop(index, None)
            yield from effect()
        yield from self.walk_to(len(PATH) - 1)

        result, proto, end = t["result"], t["protocol"], t["end"]
        self.say("Course complete!" if not result["error"] else "GAME OVER",
                 FULL_REPORT_HINT)
        b = t.get("buffers", {}).get("summary", {})
        self.overlay = [
            ("STATEMENT CLEAR!" if not result["error"] else "STATEMENT FAILED",
             CHOMP if not result["error"] else DIRTY),
            ("", ""),
            (f"ROUND TRIP        {proto['round_trip_ms']:9.3f} ms", TEXT),
            (f"{end['command']:<10}        {end['round_trip_ms']:9.3f} ms", TEXT),
            (f"CACHE HITS               {self.hits:5d}", HIT),
            (f"DISK READS               {self.reads:5d}", READ),
            (f"PAGES MODIFIED           {b.get('modified', 0):5d}", DIRTY),
            (f"WAL BYTES             {self.wal_bytes:6d} B", WAL),
            (f"GHOSTS (locks)           {len(self.ghosts):5d}", GHOST_COLORS[0]),
        ]
        yield from self.hold(75)
