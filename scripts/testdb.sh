#!/usr/bin/env bash
# Throwaway PostgreSQL cluster for trying pgtrail, owned by the current user.
# Lives in .testdb/ next to this repo, listens only on a unix socket there
# (port 5499), and never touches the system cluster.
#
#   scripts/testdb.sh start   create on first use, then start
#   scripts/testdb.sh stop
#   scripts/testdb.sh reset   delete everything and recreate
#   scripts/testdb.sh dsn     print the connection string
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/.testdb"
DATA="$DIR/data"
PORT=5499
BIN="${PGBIN:-$(pg_config --bindir 2>/dev/null || echo /usr/lib/postgresql/16/bin)}"
# Debian's pg_config may point at /usr/bin, which lacks initdb and pg_ctl
[ -x "$BIN/initdb" ] || BIN="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"
# a Unix socket path may be at most 107 bytes, so a deep folder gets a short one in /tmp
SOCK="$DIR"
[ ${#DIR} -le 90 ] || SOCK="/tmp/pgtrail-$(id -u)-$PORT"
DSN="host=$SOCK port=$PORT dbname=pgtrail user=postgres"

init() {
    mkdir -p "$DIR"
    "$BIN/initdb" -D "$DATA" -U postgres --auth=trust -E UTF8 --locale=C.UTF-8 >/dev/null
    cat >>"$DATA/postgresql.conf" <<EOF
listen_addresses = ''
port = $PORT
unix_socket_directories = '$SOCK'
shared_buffers = 16MB
wal_level = replica
EOF
}

seed() {
    "$BIN/psql" -q -h "$SOCK" -p $PORT -U postgres -d postgres -c "create database pgtrail"
    "$BIN/psql" -q -h "$SOCK" -p $PORT -U postgres -d pgtrail <<'SQL'
create extension pageinspect;
create extension pg_buffercache;
create extension pg_walinspect;
create table accounts (
    id      int primary key,
    owner   text not null,
    balance numeric(12,2) not null default 0
);
insert into accounts
select g, 'user' || g, (g * 37 % 1000)::numeric
from generate_series(1, 10000) g;
create table orders (
    id         serial primary key,
    account_id int not null,
    amount     numeric(10,2)
);
insert into orders (account_id, amount)
select (g * 7) % 10000 + 1, g % 500
from generate_series(1, 50000) g;
create index orders_account_idx on orders (account_id);
vacuum analyze accounts, orders;
SQL
}

start() {
    local fresh=0
    [ -d "$DATA" ] || { init; fresh=1; }
    mkdir -p -m 700 "$SOCK"                 # /tmp is emptied at reboot
    "$BIN/pg_ctl" -D "$DATA" -l "$DIR/server.log" status >/dev/null 2>&1 \
        || "$BIN/pg_ctl" -D "$DATA" -l "$DIR/server.log" -w start >/dev/null
    [ $fresh = 1 ] && seed
    echo "running: $DSN"
}

stop() { [ -d "$DATA" ] && "$BIN/pg_ctl" -D "$DATA" -m fast -w stop >/dev/null || true; }

case "${1:-start}" in
    start) start ;;
    stop)  stop ;;
    reset) stop; rm -rf "$DIR"; start ;;
    dsn)   echo "$DSN" ;;
    *)     echo "usage: $0 start|stop|reset|dsn" >&2; exit 2 ;;
esac
