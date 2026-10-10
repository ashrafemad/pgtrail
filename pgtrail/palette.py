"""Exact colors for the animated themes, so they look the same in any terminal."""
import os

TRUECOLOR = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def rgb(r, g, b, fallback, bold=True):
    """24-bit color when the terminal says it has it, a basic ANSI color otherwise."""
    return f"{'1;' if bold else ''}38;2;{r};{g};{b}" if TRUECOLOR else fallback


# the themes paint their own dark background, so light terminals stay readable
BG = "48;2;16;18;28" if TRUECOLOR else "40"
TEXT = rgb(245, 240, 225, "1;37")
EXPLAIN = rgb(205, 210, 220, "37", bold=False)   # the plain-language lines
DIM = rgb(150, 150, 145, "37", bold=False)       # labels and decoration
GOLD = rgb(250, 205, 70, "1;33")

# one color per kind of event, shared by every theme
QUERY = rgb(80, 205, 230, "1;36")       # a protocol message
READ = rgb(250, 170, 60, "1;33")        # page read from disk (buffer miss)
HIT = rgb(110, 215, 110, "1;32")        # page already in shared buffers
DIRTY = rgb(235, 85, 70, "1;31")        # page modified in memory
WAL = rgb(205, 115, 230, "1;35")        # WAL record
DISK = rgb(100, 155, 245, "1;34")       # written to disk (fsync, checkpoint)
