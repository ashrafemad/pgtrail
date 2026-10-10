"""Maps on-disk identifiers (tablespace/database/filenode) to relation names."""

_KINDS = {"r": "table", "i": "index", "t": "toast", "m": "matview", "S": "sequence"}
FORKS = {0: "main", 1: "fsm", 2: "vm", 3: "init"}
SEGMENT_BYTES = 1 << 30  # data files are split into 1 GB segments (RELSEG_SIZE)


class Rels:
    def __init__(self, conn):
        self.conn = conn
        self.db_oid = conn.execute(
            "select oid from pg_database where datname = current_database()"
        ).fetchone()[0]
        self.data_directory = conn.execute("show data_directory").fetchone()[0]
        self.block_size = int(conn.execute("show block_size").fetchone()[0])
        self._cache = {}
        self._tablespaces = {}

    def file(self, db, tablespace, filenode, fork="main", block=0):
        """Absolute path of the data file holding this block."""
        if tablespace == 1664:          # pg_global
            path = f"global/{filenode}"
        elif tablespace == 1663:        # pg_default
            path = f"base/{db}/{filenode}"
        else:
            path = f"{self._tablespace_dir(tablespace)}/{db}/{filenode}"
        if fork != "main":
            path += f"_{fork}"
        segment = block * self.block_size // SEGMENT_BYTES
        if segment:
            path += f".{segment}"
        return f"{self.data_directory}/{path}"

    def _tablespace_dir(self, tablespace):
        if tablespace not in self._tablespaces:
            version_dir = self.conn.execute(
                "select d from pg_ls_dir(%s) d where d like 'PG\\_%%' limit 1",
                (f"pg_tblspc/{tablespace}",),
            ).fetchone()
            self._tablespaces[tablespace] = (
                f"pg_tblspc/{tablespace}" + (f"/{version_dir[0]}" if version_dir else ""))
        return self._tablespaces[tablespace]

    def by_filenode(self, db, tablespace, filenode):
        key = (db, tablespace, filenode)
        if key in self._cache:
            return self._cache[key]
        info = {
            "name": f"filenode {filenode} (database {db})",
            "kind": "unknown",
            "am": "",
            "system": False,
            "inspectable": False,
        }
        if db in (0, self.db_oid):
            row = self.conn.execute(
                """
                select c.oid::regclass::text, c.relkind::text,
                       coalesce(am.amname, ''), c.oid < 16384
                from pg_class c left join pg_am am on am.oid = c.relam
                where c.oid = pg_filenode_relation(%s, %s)
                """,
                (tablespace, filenode),
            ).fetchone()
            if row:
                info = {
                    "name": row[0],
                    "kind": _KINDS.get(row[1], row[1]),
                    "am": row[2],
                    "system": row[3],
                    "inspectable": db == self.db_oid,
                }
        self._cache[key] = info
        return info
