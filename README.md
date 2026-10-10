# pgtrail

Follow one SQL statement through PostgreSQL, layer by layer, on a real instance.

```
pgtrail "update accounts set balance = balance + 10 where id = 42"
```

The statement really runs, on its own backend, while a second connection
watches from outside. You see what crossed the wire, how long parsing,
planning and execution took, which plans the planner compared, the locks it
took, the pages it read and changed in memory, the WAL records it wrote, the
bytes on those pages, the commit's flush to disk, and the files on disk behind
all of it. Nothing is simulated: every number is measured.

It prints a full report, or replays the same trace as a terminal animation
(three themes), or shows how fast it all really was.

- [Quick start](#quick-start)
- [What you need](#what-you-need)
- [Step by step: the first run](#step-by-step-the-first-run)
- [Using your own PostgreSQL](#using-your-own-postgresql)
- [Ways to run it](#ways-to-run-it)
- [Presenting it](#presenting-it)
- [Example queries](#example-queries)
- [All options](#all-options)
- [What the report shows](#what-the-report-shows)
- [Two sessions at once](#two-sessions-at-once)
- [Troubleshooting](#troubleshooting)
- [How the code is laid out](#how-the-code-is-laid-out)
- [What it does not cover yet](#what-it-does-not-cover-yet)

## Quick start

On Linux, with PostgreSQL 15 or newer installed, from this folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
scripts/testdb.sh start
export PGTRAIL_DSN="$(scripts/testdb.sh dsn)"
.venv/bin/pgtrail "update accounts set balance = balance + 10 where id = 42"
```

In the fish shell, replace the `export` line with
`set -x PGTRAIL_DSN (scripts/testdb.sh dsn)`.

## What you need

- **Linux.** Tested on Ubuntu with PostgreSQL 16. Other versions, Docker and
  macOS are untested.
- **Python 3.10 or newer.** The only dependency, `psycopg`, is installed for you.
- **PostgreSQL 15 or newer, with the contrib modules** (`pageinspect`,
  `pg_buffercache`, `pg_walinspect`, `auto_explain`). On Ubuntu and Debian the
  `postgresql` package includes them: `sudo apt install postgresql`.
- **A superuser connection to a throwaway instance. Never production.** pgtrail
  creates three extensions, switches on extra logging for its own session, and
  the statement you give it really runs (and commits, unless you pass `--rollback`).
- **For the animations:** a terminal with UTF-8, at least 80x24 for the classic
  theme and 100x30 for `catch` and `maze`. Exact colours need a terminal that
  sets `COLORTERM=truecolor`, which most modern ones do.
- **Optional, for `--os`:** `strace`, and permission to trace the server's processes.

## Step by step: the first run

### 1. Install pgtrail

```bash
cd pgtrail
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pgtrail --version
```

The examples below use `.venv/bin/pgtrail`. If you activate the virtual
environment (`source .venv/bin/activate`, or `.venv/bin/activate.fish` in fish),
plain `pgtrail` works too.

### 2. Start a test database

`scripts/testdb.sh` creates a separate PostgreSQL instance just for pgtrail. It
lives in `.testdb/` inside this folder, belongs to your user (no sudo), and
only listens on a socket file in that folder (port 5499). It never touches the
PostgreSQL your system may already run.

```bash
scripts/testdb.sh start
```

The first start creates the instance and a database `pgtrail` with:

| Table | Rows | Indexes |
|---|---|---|
| `accounts (id, owner, balance)` | 10,000 | primary key on `id` |
| `orders (id, account_id, amount)` | 50,000 | primary key on `id`, `orders_account_idx` on `account_id` |

The other commands:

| Command | What it does |
|---|---|
| `scripts/testdb.sh start` | create it on first use, then start it |
| `scripts/testdb.sh stop` | stop it |
| `scripts/testdb.sh reset` | delete everything and create it again |
| `scripts/testdb.sh dsn` | print its connection string |

It is not started at boot: run `scripts/testdb.sh start` after a reboot.
Stopping and starting it also empties its memory, which is how you get
pages read from disk instead of found in memory.

### 3. Tell pgtrail where to connect

```bash
export PGTRAIL_DSN="$(scripts/testdb.sh dsn)"
```

pgtrail looks for the connection in this order: `-d` / `--dsn` on the command
line, then `$PGTRAIL_DSN`, then the usual libpq settings (`PGHOST`, `PGPORT`,
`PGUSER`, `PGDATABASE`, `~/.pgpass`). To use another instance, pass any libpq
connection string, for example
`-d "host=localhost user=postgres password=postgres dbname=postgres"`.

### 4. Run a statement

```bash
.venv/bin/pgtrail "update accounts set balance = balance + 10 where id = 42"
```

You get the full report, section by section (see
[What the report shows](#what-the-report-shows)). The update is committed; add
`--rollback` to undo it after tracing.

### 5. Watch it

```bash
.venv/bin/pgtrail --theme catch --checkpoint "update accounts set balance = balance + 10 where id = 42"
```

While it plays: **space** skips to the next step, **p** pauses, **q** quits.

## Using your own PostgreSQL

The test database is the easy start, but pgtrail works with any PostgreSQL 15
or newer that you are allowed to experiment on: a local install, a Docker
container, or a copy of your application's development database. **Not
production:** the statement really runs, pgtrail switches on extra logging for
its own session, and `--checkpoint` and `--lock-wait` affect the whole server.

### 1. Have PostgreSQL 15+ with its contrib modules

pgtrail needs four modules that ship with PostgreSQL but are sometimes packaged
separately: `pageinspect`, `pg_buffercache`, `pg_walinspect` and `auto_explain`.

| Where it runs | How to get it |
|---|---|
| Ubuntu / Debian | `sudo apt install postgresql` (the modules are included) |
| Fedora / RHEL | `sudo dnf install postgresql-server postgresql-contrib` |
| macOS (Homebrew) | `brew install postgresql@16` (included) |
| Docker | `docker run -d --name pg -e POSTGRES_PASSWORD=secret -p 5433:5432 postgres:16` (the official image includes them) |
| Already installed | check with `psql -c "select name from pg_available_extensions where name in ('pageinspect', 'pg_buffercache', 'pg_walinspect')"`: three rows means you're set |

Docker and macOS are not tested yet; please report what you find.

### 2. Connect as a superuser

`pageinspect`, the stage statistics and `auto_explain` are superuser-only. Use
the `postgres` role, or create your own:

```bash
sudo -u postgres psql -c "create role $USER superuser login password 'choose-one'"
```

In the Docker example above, the `postgres` user with password `secret` already is one.

### 3. Pick a database to trace in

Use an existing one, or make one for experiments and load some tables:

```bash
createdb lab
psql lab -c "create table t (id int primary key, name text);
             insert into t select g, 'n' || g from generate_series(1, 10000) g;
             analyze t;"
```

### 4. Point pgtrail at it

```bash
export PGTRAIL_DSN="host=localhost port=5432 user=postgres password=choose-one dbname=lab"
pgtrail --rollback "select * from t where id = 42"
```

(fish: `set -x PGTRAIL_DSN "host=…"`.) Any libpq connection string works, for
example `postgresql://postgres:secret@localhost:5433/postgres` for the Docker
one, and so do the usual `PGHOST`, `PGUSER`, `PGDATABASE` variables and
`~/.pgpass`. A server on another machine works too, with two limits: the file
paths pgtrail prints are on that machine, and `--os` (strace) only works when
pgtrail runs on the database host.

### What pgtrail leaves behind

- **Three extensions** in the database it connects to: `pageinspect`,
  `pg_buffercache` and `pg_walinspect`. They only add functions; remove them with
  `drop extension pageinspect, pg_buffercache, pg_walinspect;`.
- **Your statement's effects**, unless you pass `--rollback`.
- Nothing else: the logging settings are for its own session only and end with it.

### For clean results

- **A quiet server.** The buffer cache numbers are a before/after comparison, so
  other sessions' work shows up in them.
- **`--rollback` while learning**, so every run sees the same data.
- **Restart the server** (or `sudo systemctl restart postgresql`) to empty the
  cache, to see pages read from disk instead of found in memory.

## Ways to run it

| You want | Run |
|---|---|
| The full text report | `pgtrail "<sql>"` |
| The same, and undo the change | `pgtrail --rollback "<sql>"` |
| The animation, classic look | `pgtrail --theme classic "<sql>"` |
| The animation as a monster-catching game | `pgtrail --theme catch "<sql>"` |
| The animation as a maze chase | `pgtrail --theme maze "<sql>"` |
| More time to read each step | add `--pace slow` or `--pace slower` |
| How fast it really was, no animation | `pgtrail --pace actual "<sql>"` |
| The raw trace as JSON | `pgtrail --json "<sql>"` |
| Just the essentials, on one screen | `pgtrail --detail summary "<sql>"` |
| Another session at the same time | `pgtrail --with "<other sql>" "<sql>"` |
| A deadlock, and how PostgreSQL breaks it | see [Two sessions at once](#two-sessions-at-once) |

Any `--theme`, and any `--pace` except `actual`, starts the animation. With a
pace and no theme, the theme is `classic`.

### The animation themes

- **`classic`** (80x24): packets travel between client and backend, the stages
  light up, pages rise from disk into shared buffers and turn red when
  modified, WAL records turn green when they reach disk, and with
  `--checkpoint` the dirty pages travel back down to their data files.
- **`catch`** (100x30): a monster-catching adventure. Pages are wild creatures
  in the tall grass (the data files). A page read from disk is caught with a
  ball and joins your party (shared buffers); a page already in memory is sent
  out from the party. Locks are badges, the WAL is the save file, COMMIT's
  flush is "Game saved!", and a checkpoint heals dirty pages back to disk.
- **`maze`** (100x30): a maze chase. Every event in the trace is a pellet,
  eaten in order across three lanes (wire and planning, execution, commit).
  Locks are ghosts that chase the chomper while held; the COMMIT flush is the
  power pellet that sends them home.

In `catch` and `maze`, each ball or pellet colour is one kind of event (query,
disk read, cache hit, modified, WAL, to disk), and every playful line in the
text box has a plain explanation under it. All three themes show the plans the
planner compared during the PLAN stage, and the data file or WAL file behind
each page as it is read or written.

### Paces and keys

| Pace | What it does |
|---|---|
| `normal` | each step stays up long enough to read, based on its length |
| `slow` | about 1.25 times longer per step |
| `slower` | about 2.2 times longer per step |
| `actual` | no animation: prints at once how long it really took |

While an animation plays: space for the next step, `p` to pause and resume, `q` or
Ctrl-C to quit. The last frame stays in the terminal.

## Presenting it

The animations make a simple query look like a long journey, which is the
point; `--pace actual` is the punchline.

1. Start the test database and a terminal of at least 100x30.
2. Walk through one statement slowly, with `--checkpoint` so the dirty pages
   are written at the end:
   ```bash
   .venv/bin/pgtrail --theme catch --pace slow --checkpoint --rollback "update accounts set balance = balance + 10 where id = 42"
   ```
   Press `p` to freeze the screen while you explain a step to your audience,
   and `p` again to go on; press space to skip a step you do not need.
3. Then the real speed:
   ```bash
   .venv/bin/pgtrail --pace actual --rollback "update accounts set balance = balance + 10 where id = 42"
   ```
   It prints, at once, something like: *21 steps in 1.157 ms*, the time of each
   stage, how many plans the planner compared, about 14 runs per screen
   refresh, and the files on disk it touched.
4. To show disk reads, empty the cache first with
   `scripts/testdb.sh stop; scripts/testdb.sh start`.

`--rollback` keeps the data the same between rehearsals.

## Example queries

All of these work on the test database. Add `--rollback` to the writes to
keep the data unchanged.

**One row, through an index:**
```sql
select * from accounts where id = 7
update accounts set balance = balance + 10 where id = 42
```

**A whole table:** a sequential scan, and the planner rejects the index.
```sql
select count(*) from accounts
```

**Several steps:** a join, a filter, grouping, sorting and a limit. The
planner compares hash joins, nested loops and merge joins, sorting against
hashing; the PLAN section lists them with their estimated costs.
```sql
select a.owner, count(*) as orders, sum(o.amount) as total
from accounts a join orders o on o.account_id = a.id
where a.balance > 900
group by a.owner
order by total desc
limit 5
```

**A subquery:**
```sql
select * from orders
where account_id in (select id from accounts where owner like 'user12%')
order by amount desc limit 10
```

**A write that reads another table first:** two indexes, two tables, and the
pages of both.
```sql
update accounts a set balance = a.balance - o.amount
from orders o where o.account_id = a.id and o.id = 123
```

**Inserts and deletes:**
```sql
insert into accounts values (20001, 'new', 0)
delete from orders where id = 500
```

Things worth trying:

- Restart the test database, then run a point `SELECT`: the pages are read
  from the OS. Run it again: only cache hits. With `--os`, the B-tree descent
  shows up as one `pread64` per level, root to leaf, then the heap page.
- `UPDATE` an indexed column versus a non-indexed one: the second is a HOT
  update and writes no index WAL record.
- Run a write right after `--checkpoint`: the first change to each page logs
  a full-page image.
- `DELETE`, then look at PAGES: the row's bytes are still there, only `xmax` is set.

## All options

```
pgtrail [options] "<sql>"
```

Options can go before or after the statement. `pgtrail --help` prints a short
version of this list.

### At a glance

| Option | Values | Default |
|---|---|---|
| `"<sql>"` | one SQL statement, in quotes | required |
| `-d`, `--dsn` | a libpq connection string | `$PGTRAIL_DSN`, then the `PG*` variables |
| `--rollback` | flag | off: the statement is committed |
| `--checkpoint` | flag | off |
| `--cold` | flag | off: the backend is warmed up first |
| `--os` | flag | off |
| `--protocol` | `extended`, `simple` | `extended` |
| `--lock-wait` | milliseconds, e.g. `300` | off: no other session (500 with `--with`) |
| `--with` | an SQL statement | none |
| `--deadlock` | an SQL statement (needs `--with`) | none |
| `--detail` | `summary`, `detailed`, `full` | `full` |
| `--max-pages` | a whole number, 0 or more | `8` |
| `--json` | flag | off |
| `--no-color` | flag | off (automatic when the output is not a terminal) |
| `--theme` | `classic`, `catch`, `maze` | none: no animation (`classic` when animating) |
| `--pace` | `normal`, `slow`, `slower`, `actual` | `normal` (when animating) |
| `--version` | flag | |
| `-h`, `--help` | flag | |

### What gets shown

pgtrail always traces the same things; these options only choose how to show
them. The first one that applies wins:

1. `--json`: the raw trace as JSON.
2. `--pace actual`: the real-speed summary.
3. `--theme`, or `--pace normal|slow|slower`: an animation.
4. Otherwise: the full text report.

### What the statement does

**`"<sql>"`.** The one statement to trace: `SELECT`, `INSERT`, `UPDATE`,
`DELETE`, `MERGE`, or anything else PostgreSQL accepts. Only one statement:
`select 1; select 2` is refused. Quote it for your shell; single quotes
inside double quotes are fine: `"select * from accounts where owner = 'user7'"`.
It really runs, inside a transaction that pgtrail opens and closes.

**`--rollback`.** Ends the transaction with `ROLLBACK` instead of `COMMIT`, so
the data is unchanged afterwards. Everything is still traced: the statement
runs in full, takes its locks, changes pages in memory and writes WAL records.
The difference is at the end:

| | without `--rollback` | with `--rollback` |
|---|---|---|
| Last WAL record | `COMMIT` | `ABORT` |
| Waits for the WAL to reach disk (fsync) | yes, this is the durability point | no, nothing has to survive a crash |
| Data afterwards | changed | unchanged: the new row versions are marked as aborted and ignored |
| Running the same statement again | sees the changed data | sees the same data every time |

Use it for rehearsals and demos, and whenever you trace a write you do not
want to keep. A statement that fails is always rolled back.

**`--checkpoint`.** Once the transaction has ended, runs a `CHECKPOINT` and
times it. A checkpoint writes every dirty page in shared buffers to its data
file and flushes them to disk. Normally that happens later, in the
background, so after a plain write the changed pages exist only in memory
and in the WAL. With `--checkpoint` you see them reach their files: the
AFTERMATH section shows them clean, the animations play the write-back
(catch: the healing center). It works with `--rollback` too, since the aborted
changes also dirtied pages.

Two things to know: the checkpoint is for the whole instance, not just your
pages, so on a busy server it takes longer and writes more. And the first
change to each page after a checkpoint writes the whole page into the WAL (a
full-page image), so the next write you trace will show bigger WAL records.

**`--cold`.** Normally pgtrail first plans the statement once with `EXPLAIN`
in a rolled-back transaction, so the backend has already loaded the catalog
information it needs (table and column definitions, types, operators).
`--cold` skips that: the trace then includes that one-off loading, which shows
up as extra catalog pages (`pg_class`, `pg_attribute`, …) in BUFFER CACHE and
slower parse, analyze and plan stages. That is what the first statement on a
new connection costs.

**`--os`.** Also traces the system calls of the backend, the checkpointer, the
background writer and the WAL writer with `strace`: every `pread64`,
`pwrite64`, `fdatasync` and socket call, translated to relations and blocks,
shown in SYSCALLS. Linux only. It must run on the database host, with
permission to trace those processes (see Troubleshooting). `strace` slows the
processes a lot, so take timings from a run without it.

**`--protocol extended|simple`.** How the statement travels to the server.
`extended`, the default, is what applications and drivers send: five messages
in one go (Parse, Bind, Describe, Execute, Sync), and the server answers each
stage as it finishes (ParseComplete after parsing and analysis, BindComplete
after planning). `simple` sends one `Query` message carrying the SQL text, as
`psql` does. Results come back in binary with `extended` and as text with `simple`.

**`--lock-wait MS`.** A demo of one session waiting for another. Before the
statement is sent, a second session locks the statement's first table in
`ACCESS EXCLUSIVE` mode, and releases it after `MS` milliseconds. The statement
has to queue for its own lock. You see how long it waited, what the server
reported while it was waiting (`Lock/relation`, blocked by which session),
and that waiting takes time but no CPU: the stage that opens the tables
(analyze) shows the wait as elapsed time with almost no CPU time. The
animations play it as a blocker that steps aside. Try `--lock-wait 300`.
With `--with`, it sets how long the other session keeps its transaction open.

**`--with SQL`.** A second session runs `SQL` first, keeps its transaction open
for `--lock-wait` milliseconds (500 if not given), then ends it the way yours
ends: COMMIT, or ROLLBACK with `--rollback`. Your statement runs meanwhile. You
see whether it had to wait, for what (a whole table, or a row the other
transaction changed), and for how long, or that it did not, and why. See
[Two sessions at once](#two-sessions-at-once).

**`--deadlock SQL`.** Needs `--with`. Builds the classic deadlock: your session
runs `SQL` first; the other session runs `--with` and then `SQL` too, which
makes it wait for you; then your traced statement waits for the other session.
Each waits for the other. PostgreSQL's deadlock detector finds the cycle after
`deadlock_timeout` (1 s by default) and cancels one of them. The other session
always rolls back at the end, so the demo leaves the data as it was.

**`--detail summary|detailed|full`.** How much to show, in the report and the
animations. `summary` is one screen: the total time, the stages on one line,
the plan in plain words, the warnings (misestimates, spills, a lock wait) and
the files touched; the animations keep only the core trip. `detailed` is every
section without the server's own trees, the page bytes, the B-tree drawings
and the syscalls; the animations skip the tree scenes but keep the plan choice
and the B-tree dive. `full`, the default, is everything. `--json` always has
everything.

**`--max-pages N`.** How many changed pages the PAGES section reads back with
`pageinspect` and shows byte by byte: table pages first, then index pages.
Only writes have changed pages. `0` shows none, only how many there were; a
larger number shows more of a big write, at the cost of a longer report.

### Where it connects

**`-d`, `--dsn`.** Any libpq connection string, for example
`"host=localhost port=5432 user=postgres password=secret dbname=postgres"` or
`"postgresql://postgres@localhost/postgres"`. Without it pgtrail uses
`$PGTRAIL_DSN`, and without that the standard libpq settings: `PGHOST`,
`PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` and `~/.pgpass`. It needs a
superuser on PostgreSQL 15 or newer. For the test database:
`-d "$(scripts/testdb.sh dsn)"`.

### Output

**`--json`.** Prints the whole trace as one JSON document instead of anything
else: every message, stage, plan, lock, page, WAL record and file path. Good
for saving a run, comparing two runs, or building another front end.

**`--no-color`.** Plain text without colour codes, for the report and the
`--pace actual` summary. Colours are already off when the output goes to a file
or a pipe. The animations always use colour.

### Animation

**`--theme`.** Replays the trace as an animation in the terminal instead of
printing the report, and picks which story it tells (see
[The animation themes](#the-animation-themes)). Giving any theme starts the
animation. Needs an interactive terminal of at least 80x24 for `classic` and
100x30 for the others; a taller terminal shows a long query in full at the
top, and any text that still does not fit is shown a page at a time
(`1/2`, `2/2`) instead of being cut off.

| `--theme` value | Screen | What it is |
|---|---|---|
| `classic` | 80x24 | client, backend, shared buffers and disk as boxes |
| `catch` | 100x30 | a monster-catching adventure |
| `maze` | 100x30 | a maze chase |

**`--pace`.** How long each step stays on screen. Any value starts the
animation, except `actual`, which shows no animation.

| Value | What it does |
|---|---|
| `normal` | each step stays up long enough to read, based on how much text it has |
| `slow` | about 1.25 times longer per step |
| `slower` | about 2.2 times longer per step |
| `actual` | no animation: prints at once the real time of each step, how many steps there were, the plans compared and the files touched |

Keys while an animation plays:

| Key | Action |
|---|---|
| space | skip to the next step |
| `p` | pause, and resume |
| `q` or Ctrl-C | quit; the last frame stays in the terminal |

### Other

**`--version`** prints the version. **`-h`, `--help`** prints the short list.

### Environment variables

| Variable | Used for |
|---|---|
| `PGTRAIL_DSN` | the connection string when `-d` is not given |
| `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` | libpq defaults when neither `-d` nor `PGTRAIL_DSN` is set |
| `COLORTERM=truecolor` | exact 24-bit colours in the animations; basic colours otherwise |
| `PGBIN` | for `scripts/testdb.sh`: the PostgreSQL `bin` folder, if it is not found |

### Common combinations

| Goal | Command |
|---|---|
| Learn what one write does, keep the data | `pgtrail --rollback "<sql>"` |
| See the changed pages written to disk | `pgtrail --rollback --checkpoint "<sql>"` |
| Rehearse a demo, same result every time | `pgtrail --theme catch --pace slow --checkpoint --rollback "<sql>"` |
| The real speed, after the demo | `pgtrail --pace actual --rollback "<sql>"` |
| One session waiting for another | `pgtrail --lock-wait 300 --rollback "<sql>"` |
| Two writers on the same row | `pgtrail --with "update accounts set balance = 0 where id = 7" --rollback "update accounts set balance = 1 where id = 7"` |
| A deadlock | see [Two sessions at once](#two-sessions-at-once) |
| A quick look | `pgtrail --detail summary "<sql>"` |
| What a brand new connection costs | `pgtrail --cold "select * from accounts where id = 7"` |
| Disk reads and system calls | restart the test database, then `sudo -E pgtrail --os "<sql>"` |
| Save a run to compare later | `pgtrail --json --rollback "<sql>" > run.json` |

## Two sessions at once

Locks and deadlocks happen between sessions. `--with` adds a second session
that does its work first and keeps its transaction open while yours runs. Every
case below was traced on the test database (`--rollback` keeps the data the same).

**Two writers on the same row:** your UPDATE waits for the other transaction.

```bash
pgtrail --rollback --with "update accounts set balance = balance - 1 where id = 7" \
        "update accounts set balance = balance + 1 where id = 7"
```

It waits about 500 ms for a `ShareLock` on the other transaction: a row lock
waits for the *whole* other transaction to end. The execute stage shows 500 ms
of elapsed time and well under 1 ms of CPU: waiting costs time, not CPU. If the
other session commits (run it without `--rollback`), your UPDATE then re-reads
the row's new version before changing it.

**Two writers on different rows:** no wait at all.

```bash
pgtrail --rollback --with "update accounts set balance = balance - 1 where id = 8" \
        "update accounts set balance = balance + 1 where id = 7"
```

**A reader and a writer on the same row:** no wait either. The SELECT gets the
last committed version of the row (MVCC: readers never wait for writers).

```bash
pgtrail --rollback --with "update accounts set balance = balance - 1 where id = 7" \
        "select * from accounts where id = 7"
```

**A deadlock:** two transfers between accounts 1 and 2, in opposite order.

```bash
pgtrail --rollback \
        --with     "update accounts set balance = balance - 10 where id = 1" \
        --deadlock "update accounts set balance = balance + 10 where id = 2" \
                   "update accounts set balance = balance + 10 where id = 1"
```

1. Your session updates account 2.
2. The other session updates account 1, then tries account 2: it waits for you.
3. Your traced statement tries account 1: it waits for the other session.

Each waits for the other. After `deadlock_timeout` (1 s), PostgreSQL's deadlock
detector cancels one of them, usually the one that started waiting first, here
the other session. pgtrail shows both waits as the server reported them, the
server's own deadlock message with the cycle, who was cancelled, and that your
statement then finished. In the games: a rival trainer, a blocking ghost, or two
players each stuck waiting for the other.

## What the report shows

| # | Section | What it shows | Where it comes from |
|---|---------|---------------|---------------------|
| 1 | WIRE | Every protocol message, with timing, size and what it is for | libpq `PQtrace` |
| 2 | STAGES | Time, CPU time and page faults in parse, analyze, rewrite, plan and execute; then the server's own query tree and plan tree | `log_parser_stats`, `log_planner_stats`, `log_executor_stats`; `debug_print_parse`, `debug_print_rewritten`, `debug_print_plan` |
| 3 | PLAN | The executed plan with rows, buffers and WAL per node; where the planner's row estimates were off; rows a filter threw away; sorts and hashes in memory or spilled to disk, and temp files; the plans the planner compared, with costs; the statistics behind the estimates; each B-tree index used, and the lookup walked from root to leaf | `auto_explain`, `EXPLAIN` with `enable_*` settings, `pg_stats`, `pageinspect` |
| 4 | LOCKS | Locks held when the statement finished; with `--lock-wait`, `--with` or `--deadlock`, whether it waited, for what and for how long, and the deadlock the server found | `pg_locks`, `pg_stat_activity`, the server's deadlock report |
| 5 | BUFFER CACHE | Pages read in, hit, modified or dirtied, catalogs included, and each relation's data file | `pg_buffercache`, diffed before and after |
| 6 | WAL | The log records the statement wrote, and the WAL file they went to | `pg_walinspect` |
| 7 | PAGES | Line pointers, new and old row versions, index entries | `pageinspect` |
| 8 | COMMIT | The commit round trip and the WAL flush | libpq `PQtrace` |
| 9 | AFTERMATH | Pages still dirty in memory; optional forced checkpoint | `pg_buffercache` |
| 10 | SYSCALLS | Every file and socket syscall, translated to relations and blocks | `strace` (with `--os`) |

**Plans considered.** PostgreSQL keeps only the cheapest plan and discards
the others, so pgtrail recovers the runners-up the way a DBA would: it plans
the statement again with the winner's methods switched off (`enable_indexscan
= off` and so on), one at a time and then two at a time. Each is `EXPLAIN`
only, run after the trace, so nothing extra executes and no measured number
changes. The costs are the planner's estimates, in its own units (about 1 per
page read in sequence), not measured times. The report prints the settings to
reproduce the last one yourself.

**For tuning queries**, the PLAN section adds what EXPLAIN ANALYZE users look
for, in words:

- **Estimates vs reality:** each step's guessed and actual rows; a step more
  than 2× off is called out, and the worst one is summed up. (A step below a
  `LIMIT` that stops early is not counted as wrong.)
- **Wasted work:** "read 10,000 rows, threw away 9,010 (90%)": a filter doing
  an index's job.
- **Memory:** each sort and hash table, in memory or spilled to disk, with the
  temp files it wrote, against `work_mem`.
- **The statistics behind the estimates:** for every column the plan filters,
  joins, groups or sorts on: rows and pages, distinct values, most common
  values, histogram, and how well the table's order matches the column.
- **B-tree lookups:** the index's height and size, and for `id = 42` the walk
  itself: root page → leaf page → row, including old entries still waiting for
  VACUUM, deduplicated entries, and HOT chains that lead to the live row. A
  tree of up to three levels is also drawn, page by page, with each branch's
  key range; the way down to your row is colored.
- **No index used:** when the plan reads tables in full, it says so, and the
  plan comparison shows how much more an index would have cost.

**File paths** are on the database server, under its data directory:
`base/<database>/<filenode>` for tables and indexes, with `_fsm` and `_vm` for
their free space and visibility maps, `.1`, `.2` for each further gigabyte,
and `pg_wal/` for the WAL.

## Troubleshooting

| Message or symptom | What to do |
|---|---|
| `cannot connect` | Is the instance running (`scripts/testdb.sh start`)? Is `PGTRAIL_DSN` set in this shell? |
| `Connect as a superuser` | pgtrail needs a superuser; the test database connects as `postgres`, which is one |
| `Cannot create extension ...` | install the contrib modules for your PostgreSQL version |
| `PostgreSQL 15 or newer is required` | `pg_walinspect` arrived in 15 |
| `this animation needs a terminal of at least 100x30` | enlarge the window or the font size, or use the classic theme (80x24) |
| `the animation needs an interactive terminal` | animations cannot be piped; use the text report or `--json` |
| Colours look plain | run `export COLORTERM=truecolor` (fish: `set -x COLORTERM truecolor`) |
| No pages read from disk, only hits | the pages were already in memory; restart the test database first |
| `--os`: strace cannot attach | allow tracing until the next reboot with `sudo sysctl kernel.yama.ptrace_scope=0`, or run pgtrail with `sudo -E`; in Docker add `--cap-add=SYS_PTRACE` |
| `scripts/testdb.sh` cannot find `initdb` | set `PGBIN` to your PostgreSQL `bin` folder, e.g. `PGBIN=/usr/lib/postgresql/16/bin scripts/testdb.sh start` |

## How the code is laid out

```
pgtrail/
  cli.py               command line
  tracer.py            runs the statement and calls the collectors in order
  render.py            text report; reads the trace dict only
  actual.py            --pace actual: the real timing, printed at once
  arcade.py            the animation player and the classic theme
  catch.py, maze.py    the other themes, same trace dict
  palette.py           the themes' colours
  rels.py              filenode → relation name and data file path
  collectors/
    protocol.py        layer 1  wire protocol
    stages.py          layer 2  parse / plan / execute, and the plan
    plans.py           layer 2b the plans the planner compared
    trees.py           layer 2c the server's query, rewritten and plan trees
    statistics.py      layer 2d the statistics the planner estimated from
    contention.py      layer 3b another session holding a lock (--lock-wait)
    btree.py           layer 6b the B-tree indexes used, walked root to leaf
    locks.py           layer 3  lock manager
    buffers.py         layer 4  buffer manager
    wal.py             layer 5  write-ahead log
    pages.py           layer 6  page contents
    syscalls.py        layer 7  operating system
scripts/
  testdb.sh            the throwaway test instance
```

Every collector returns plain dicts and lists, and the whole trace is one JSON
document (`--json`). The report, the animations and `--pace actual` only read
that document, so a web UI would only replace them. Adding a layer means a
collector and a section in each view.

## What it does not cover yet

- **Block device I/O.** `pread64` shows a read reaching the OS, not whether it
  was served by the page cache or the disk. That needs eBPF on the block layer
  (`block:block_rq_issue`), which would also give sector offsets and real
  seek distances.
- **Exact buffer accesses.** The buffer diff relies on usage counts, which stop
  at 5. An ordered list of every buffer pin needs uprobes on the buffer manager.
- **CPU caches.** CPU time and page faults are shown per stage; cache misses
  and instruction counts need `perf stat`, which this kernel only allows root.
- **The raw parse tree.** The trees shown start after parse analysis; the raw
  grammar output before it is only printed by a specially compiled server.
- **B-tree lookups beyond one integer key.** The root-to-leaf walk is shown for
  an equality lookup on a single integer column (`id = 42`); other indexes show
  their height, size and root only.
- **Why an estimate came out as it did.** The statistics are shown; the
  selectivity arithmetic the planner did with them is not.
- **The planner's full search.** The plans considered are runners-up found by
  switching methods off, not every path the planner costed internally.
- **Named prepared statements.** The extended protocol is shown with an
  unnamed statement, sent and run in one go; reusing a `PREPARE`d statement
  (and its cached plan) is not traced.
- **More than two sessions.** `--with` and `--deadlock` stage one other
  session; three-way deadlocks, lock queues and long-running contention on a
  busy server need the server log (`log_lock_waits`) and `pg_stat_activity`.
- **Other sessions.** On a busy instance the buffer diff includes their activity.
  Use a quiet instance.

## Known side effects of the instrumentation

- The stage statistics, the plan and the trees arrive as extra
  `NoticeResponse` messages (tens of kilobytes for the trees). They are
  labelled in WIRE and SYSCALLS, and left out of the step count in
  `--pace actual`, which also skips the trees so its timings stay close to a
  plain run.
- `auto_explain` with timing adds overhead to the execute stage, so a plain
  run is a little faster than the numbers shown.
- `strace` slows the traced processes considerably, so read timings from a run
  without `--os`.
