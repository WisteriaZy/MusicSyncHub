"""SQLite 索引库。

所有连接都跑在各自线程里（FastAPI 线程池 + 扫描线程池），
因此连接按线程局部保存，并开启 WAL 以支持并发读写。
"""
from __future__ import annotations

import atexit
import sqlite3
import threading
import logging
import time
import pathlib
from contextlib import contextmanager
from pathlib import Path
from typing import Union, Any, Dict, Iterable, Iterator, List, Optional, Sequence

# `paths` 只依赖标准库，不会有循环导入问题。
# 索引里存的路径一律是归一化形式（`P.norm`），查询时必须同样归一化，
# 否则 `D:\Music\x` 和 `D:/Music/x` 会被当成两条不同的记录。
from . import paths as P

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS source (
  id          TEXT PRIMARY KEY,
  name        TEXT,
  type        TEXT,
  root        TEXT,
  last_scan   REAL,
  track_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS track (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id     TEXT NOT NULL,
  path          TEXT NOT NULL,
  rel_path      TEXT NOT NULL DEFAULT '',
  name          TEXT NOT NULL,
  parent        TEXT NOT NULL DEFAULT '',
  ext           TEXT NOT NULL DEFAULT '',
  size          INTEGER NOT NULL DEFAULT 0,
  mtime         REAL NOT NULL DEFAULT 0,
  -- 元数据
  title         TEXT, artist TEXT, album TEXT, album_artist TEXT,
  track_no      TEXT, year TEXT, genre TEXT,
  duration      REAL, bitrate INTEGER, sample_rate INTEGER, channels INTEGER,
  bits_per_sample INTEGER, codec TEXT, is_lossless INTEGER DEFAULT 0,
  has_cover     INTEGER DEFAULT 0,
  cover_mime    TEXT, cover_hash TEXT,
  meta_status   TEXT DEFAULT 'pending',   -- ok | partial | error | skipped
  meta_error    TEXT,
  -- 计算字段
  name_key      TEXT DEFAULT '',
  dup_key       TEXT DEFAULT '',
  quality       REAL DEFAULT 0,
  -- 哈希
  hash_quick    TEXT, hash_full TEXT, hash_state TEXT DEFAULT 'none',
  scanned_at    REAL DEFAULT 0,
  UNIQUE(source_id, path)
);
CREATE INDEX IF NOT EXISTS idx_track_source ON track(source_id);
CREATE INDEX IF NOT EXISTS idx_track_namekey ON track(source_id, name_key);
CREATE INDEX IF NOT EXISTS idx_track_dupkey ON track(dup_key);
CREATE INDEX IF NOT EXISTS idx_track_hash ON track(hash_quick);

CREATE TABLE IF NOT EXISTS cover (
  hash    TEXT PRIMARY KEY,
  mime    TEXT,
  width   INTEGER,
  height  INTEGER,
  data    BLOB,
  bytes   INTEGER,
  created REAL
);

CREATE TABLE IF NOT EXISTS job (
  id        TEXT PRIMARY KEY,
  kind      TEXT,
  state     TEXT,
  total     INTEGER DEFAULT 0,
  done      INTEGER DEFAULT 0,
  message   TEXT,
  started   REAL,
  finished  REAL,
  detail    TEXT
);

-- 索引库的单调版本号 / 累计删除数。见 `Database.write()` 里的说明。
CREATE TABLE IF NOT EXISTS state_meta (
  k TEXT PRIMARY KEY,
  v INTEGER NOT NULL DEFAULT 0
);
"""


log = logging.getLogger("msh.db")

class Database:
    def __init__(self, path: Union[Path, str]):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        # 记录所有线程里的连接，方便 close_all() 一次性关干净
        # （threading.local 里的连接从其它线程看不到）。
        self._all_conns: Dict[int, sqlite3.Connection] = {}
        self._conns_lock = threading.RLock()
        with self.connect() as conn:
            try:
                self._init_schema(conn)
            except sqlite3.DatabaseError as exc:
                # ★ 索引库文件损坏（断电、蓝屏、磁盘坏道、被别的程序写坏）时，
                #   以前会直接抛 `file is not a database` → **应用启动崩溃**，
                #   而且没有任何界面能告诉用户该删哪个文件。
                #   做法和 config.toml 一致：备份坏文件 → 重建空库 → 继续启动。
                #   （索引是可以重建的：重扫一遍数据源就回来了；配置文件不行。）
                msg = str(exc).lower()
                if "not a database" not in msg and "malformed" not in msg:
                    raise
                try:
                    self.close_all()
                except Exception:                      # noqa: BLE001
                    pass
                import datetime as _dt
                stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                base = pathlib.Path(self.path)
                for suffix in ("", "-wal", "-shm"):
                    f = base.with_name(base.name + suffix)
                    if f.exists():
                        try:
                            f.replace(base.with_name(base.name + suffix + f".broken-{stamp}"))
                        except OSError:
                            pass
                log.warning("索引库文件损坏（%s），已备份并重建：%s", exc, self.path)
                print(f"[db] ★ 索引库损坏，已备份原文件并重建。请重新扫描一次数据源。")
                try:
                    with self.connect() as conn2:
                        self._init_schema(conn2)
                except sqlite3.DatabaseError as exc2:
                    # 坏文件改名失败（被另一个实例/杀毒软件占着）时，
                    # 再建库还是会失败。给一句能照做的提示，别只抛原始错误。
                    raise RuntimeError(
                        "索引库文件已损坏，且无法自动重建（文件可能正被另一个"
                        f" Music Sync Hub 实例或杀毒软件占用）。请关闭其他窗口后，"
                        f"手动删除或重命名这个文件再启动：{self.path}"
                    ) from exc2
        # ★ 进程退出时把连接关干净。
        #
        #   不注册的话，sqlite 的文件句柄会一直挂到解释器退出 ——
        #   在 Windows 上的表现是**临时目录删不掉**
        #   （`WinError 32: 另一个程序正在使用此文件`）。
        #   测试里到处要显式 `db.close_all()` 就是被这个逼的；
        #   生产上换数据源（rebind）之后，旧 Database 的连接也会一直
        #   留着不释放。
        #
        #   只记弱引用：Database 被回收之后这个回调就自己失效，
        #   不会拦着对象被 GC。
        try:
            import weakref
            self_ref = weakref.ref(self)

            def _close_at_exit() -> None:
                inst = self_ref()
                if inst is not None:
                    try:
                        inst.close_all()
                    except Exception:
                        pass

            atexit.register(_close_at_exit)
        except Exception:
            pass

    # ------------------------------------------------------------ 连接
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns[threading.get_ident()] = conn
        return conn

    def close_all(self) -> None:
        """关闭所有线程的连接（测试收尾 / 需要释放文件句柄时用）。"""
        with self._conns_lock:
            conns = list(self._all_conns.values())
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        try:
            self._local.conn = None
        except Exception:
            pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        yield conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        with self._write_lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            # ★ 每次真正提交就 +1 —— 这是 `/api/state` 的 revision 来源。
            #   以前 revision 是「曲目数 + WAL 的 mtime」，纯元数据更新
            #   （改标题/作者）在 0.1 秒内连改多次时**检不出来**（实测 12 次漏 11 次），
            #   前端就不会刷新。用库里的计数器才可靠且单调。
            try:
                conn.execute(
                    "INSERT INTO state_meta (k, v) VALUES ('rev', 1) "
                    "ON CONFLICT(k) DO UPDATE SET v = v + 1")
                conn.commit()
            except sqlite3.Error:
                pass

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """建表 → 补列 → 建索引。**顺序不能反**。

        ## 为什么不能直接 `executescript(SCHEMA)`

        `SCHEMA` 里既有 `CREATE TABLE` 也有 `CREATE INDEX`。老版本的库
        没有 `name_key` / `dup_key` / `hash_quick` 这几列，而索引就建在这些列上：

            sqlite3.OperationalError: no such column: name_key

        → 应用**启动失败**。用户从旧版升级过来会直接打不开（实测三种缺列都这样）。

        正确顺序是：建表（IF NOT EXISTS）→ `_migrate` 补列 → 建索引。
        这样老库能平滑升级，新库也没问题。

        另外 `executescript` 会隐式提交，多个进程/线程同时首次初始化同一个库时
        容易撞上 `database is locked`。这里加一次带退避的重试。
        """
        stmts = [s.strip() for s in SCHEMA.split(";") if s.strip()]
        is_index = [("CREATE INDEX" in s.upper() or
                     "CREATE UNIQUE INDEX" in s.upper()) for s in stmts]

        last: Optional[Exception] = None
        for attempt in range(5):
            try:
                for s, idx in zip(stmts, is_index):
                    if not idx:
                        conn.execute(s)
                conn.commit()
                self._migrate(conn)
                for s, idx in zip(stmts, is_index):
                    if idx:
                        conn.execute(s)
                conn.commit()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                last = exc
                time.sleep(0.05 * (2 ** attempt))
        if last:                                          # pragma: no cover
            raise last

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(track)")}
        wanted = {
            "hash_quick": "TEXT", "hash_full": "TEXT", "hash_state": "TEXT DEFAULT 'none'",
            "rel_path": "TEXT NOT NULL DEFAULT ''", "name_key": "TEXT DEFAULT ''",
            "dup_key": "TEXT DEFAULT ''", "quality": "REAL DEFAULT 0",
        }
        for col, decl in wanted.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE track ADD COLUMN {col} {decl}")
        conn.commit()

    # ------------------------------------------------------------ 通用
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn().execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return list(self._conn().execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        row = self._conn().execute(sql, params).fetchone()
        return row

    # ------------------------------------------------------------ 业务
    def revision(self) -> int:
        """索引库的单调版本号（每次提交 +1）。读不到就回 0。"""
        try:
            row = self.query_one("SELECT v FROM state_meta WHERE k='rev'")
            return int(row["v"]) if row else 0
        except Exception:                              # noqa: BLE001
            return 0

    def deleted_total(self) -> int:
        """累计从索引里删掉/移除的曲目数（近似，只增不减）。"""
        try:
            row = self.query_one("SELECT v FROM state_meta WHERE k='del'")
            return int(row["v"]) if row else 0
        except Exception:                              # noqa: BLE001
            return 0

    def upsert_source(self, sid: str, name: str, type_: str, root: str) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO source(id,name,type,root) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,type=excluded.type,root=excluded.root",
                (sid, name, type_, root),
            )

    def delete_source(self, sid: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM track WHERE source_id=?", (sid,))
            conn.execute("DELETE FROM source WHERE id=?", (sid,))

    def delete_tracks_by_paths(self, sid: str, paths: List[str]) -> int:
        """按路径删掉某个数据源的索引记录（跨库传输后源侧要清）。

        不清的话会出现"文件已经搬走了、搜索还能搜到"，点进去播放失败。
        返回删掉的条数。
        """
        if not paths:
            return 0
        n = 0
        with self.write() as conn:
            for i in range(0, len(paths), 200):
                chunk = [P.norm(p) for p in paths[i:i + 200] if p]
                if not chunk:
                    continue
                qs = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f"DELETE FROM track WHERE source_id=? AND path IN ({qs})",
                    (sid, *chunk))
                n += cur.rowcount or 0
        return n

    def set_scan_meta(self, sid: str, last_scan: float, count: int) -> None:
        with self.write() as conn:
            conn.execute(
                "UPDATE source SET last_scan=?, track_count=? WHERE id=?", (last_scan, count, sid)
            )

    def upsert_tracks(self, rows: List[Dict[str, Any]]) -> int:
        """按 (source_id, path) 批量写入/更新。"""
        if not rows:
            return 0
        fields = [
            "source_id", "path", "rel_path", "name", "parent", "ext", "size", "mtime",
            "title", "artist", "album", "album_artist", "track_no", "year", "genre",
            "duration", "bitrate", "sample_rate", "channels", "bits_per_sample", "codec",
            "is_lossless", "has_cover", "cover_mime", "cover_hash", "meta_status", "meta_error",
            "name_key", "dup_key", "quality", "hash_quick", "hash_full", "hash_state", "scanned_at",
        ]
        placeholders = ",".join("?" for _ in fields)
        updates = ",".join(f"{f}=excluded.{f}" for f in fields if f not in ("source_id", "path"))
        sql = (
            f"INSERT INTO track({','.join(fields)}) VALUES({placeholders}) "
            f"ON CONFLICT(source_id,path) DO UPDATE SET {updates}"
        )
        payload = [tuple(r.get(f) for f in fields) for r in rows]
        with self.write() as conn:
            conn.executemany(sql, payload)
        return len(payload)

    def update_computed(self, source_id: str, path: str, **vals: Any) -> None:
        if not vals:
            return
        sets = ",".join(f"{k}=?" for k in vals)
        with self.write() as conn:
            conn.execute(
                f"UPDATE track SET {sets} WHERE source_id=? AND path=?",
                (*vals.values(), source_id, path),
            )

    def update_computed_many(self, items: Iterable[tuple]) -> None:
        """items: (vals_dict, source_id, path)"""
        with self.write() as conn:
            for vals, sid, path in items:
                sets = ",".join(f"{k}=?" for k in vals)
                conn.execute(
                    f"UPDATE track SET {sets} WHERE source_id=? AND path=?",
                    (*vals.values(), sid, path),
                )

    def tracks(self, source_id: str, prefix: str = "", limit: int = 0, offset: int = 0) -> List[sqlite3.Row]:
        sql = "SELECT * FROM track WHERE source_id=?"
        params: List[Any] = [source_id]
        if prefix:
            sql += " AND (parent=? OR parent LIKE ?)"
            params += [prefix, prefix.rstrip("/") + "/%"]
        sql += " ORDER BY path COLLATE NOCASE"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return self.query(sql, params)

    def all_tracks(self, source_id: str) -> List[sqlite3.Row]:
        return self.tracks(source_id)

    def track(self, source_id: str, path: str) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT * FROM track WHERE source_id=? AND path=?", (source_id, path)
        )

    def track_by_id(self, tid: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM track WHERE id=?", (tid,))

    def remove_track(self, source_id: str, path: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM track WHERE source_id=? AND path=?", (source_id, path))

    def remove_tracks(self, pairs: Iterable[tuple]) -> int:
        with self.write() as conn:
            n = 0
            for sid, path in pairs:
                cur = conn.execute("DELETE FROM track WHERE source_id=? AND path=?", (sid, path))
                n += cur.rowcount or 0
            return n

    def remove_missing(self, source_id: str, alive_paths: Iterable[str]) -> int:
        alive = set(alive_paths)
        rows = self.query("SELECT path FROM track WHERE source_id=?", (source_id,))
        dead = [r["path"] for r in rows if r["path"] not in alive]
        if dead:
            with self.write() as conn:
                conn.executemany(
                    "DELETE FROM track WHERE source_id=? AND path=?", [(source_id, p) for p in dead]
                )
        return len(dead)

    def counts_by_source(self) -> Dict[str, int]:
        rows = self.query("SELECT source_id, COUNT(*) c FROM track GROUP BY source_id")
        return {r["source_id"]: r["c"] for r in rows}

    # ------------------------------------------------------------ 封面
    def put_cover(self, h: str, mime: str, w: int, hh: int, data: bytes) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO cover(hash,mime,width,height,data,bytes,created) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(hash) DO NOTHING",
                (h, mime, w, hh, data, len(data), time.time()),
            )

    def get_cover(self, h: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM cover WHERE hash=?", (h,))

    # ------------------------------------------------------------ 任务
    def create_job(self, jid: str, kind: str, total: int = 0) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO job(id,kind,state,total,done,message,started) VALUES(?,?,?,?,0,?,?)",
                (jid, kind, "running", total, "", time.time()),
            )

    def update_job(self, jid: str, **vals: Any) -> None:
        if not vals:
            return
        sets = ",".join(f"{k}=?" for k in vals)
        with self.write() as conn:
            conn.execute(f"UPDATE job SET {sets} WHERE id=?", (*vals.values(), jid))

    def delete_job(self, jid: str) -> None:
        """删掉一条任务记录（"忽略"用；不留痕，避免重启又被恢复）。"""
        with self.write() as conn:
            conn.execute("DELETE FROM job WHERE id=?", (jid,))

    def get_job(self, jid: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM job WHERE id=?", (jid,))

    def list_jobs(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM job ORDER BY started DESC LIMIT ?", (limit,))

    def vacuum(self) -> None:
        with self.write() as conn:
            conn.execute("VACUUM")
