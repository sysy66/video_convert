import os
import sqlite3
import threading
import time

log = __import__("logging").getLogger(__name__)


class History:
    """
    转码历史记录，使用 SQLite 持久化。线程安全（单连接 + RLock）。
    公开接口与旧 JSON 版本保持一致，scanner / transcoder / main 无需改动。

    Transcoding history persisted in SQLite. Thread-safe (single connection + RLock).
    The public API mirrors the old JSON version, so scanner / transcoder / main
    require no changes.
    """

    def __init__(self, path):
        self._path = path
        self._lock = threading.RLock()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False：允许 ThreadPoolExecutor 的多个 worker 复用同一连接
        #                         （所有访问都在 RLock 内串行化）。
        # isolation_level=None：自动提交，每次写入立即持久化，替代旧的整文件 save()。
        # check_same_thread=False: let ThreadPoolExecutor workers share one connection
        #                         (all access serialised under the RLock).
        # isolation_level=None: autocommit; each write is durable immediately,
        #                       replacing the old whole-file save().
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        """建表并设置 PRAGMA。Create the table and set PRAGMAs."""
        with self._lock:
            # WAL：读不阻塞写；synchronous=NORMAL + WAL 在本地盘崩溃安全；busy_timeout 吸收锁等待。
            # WAL: readers don't block the writer; NORMAL+WAL is crash-safe on local
            # disk; busy_timeout absorbs any lock wait.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    filepath     TEXT PRIMARY KEY,   -- 文件绝对路径 / absolute file path
                    status       TEXT NOT NULL,      -- started|success|rejected|failed|not required
                    started_at   REAL,
                    completed_at REAL,
                    output_path  TEXT,
                    vmaf_score   REAL,
                    error        TEXT,
                    retry_count  INTEGER,
                    codec        TEXT,
                    skipped_at   REAL
                )
                """
            )

    def close(self):
        """
        关闭数据库连接，执行 WAL checkpoint 合并 -wal/-shm 文件后正常关闭。
        Should be called on shutdown so that .db-wal / .db-shm are merged and removed.

        Close the database connection after a WAL checkpoint, so that the
        .db-wal / .db-shm ancillary files are merged into the main .db file
        and removed.
        """
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass

    def __del__(self):
        self.close()

    def save(self):
        """
        自动提交模式下无需显式保存；保留此方法仅为兼容旧接口。
        Autocommit mode needs no explicit save; kept only for API compatibility.
        """
        pass

    def get_entry(self, filepath):
        """
        返回该文件的记录字典，不存在则返回 None。
        关键：剔除值为 NULL 的列，忠实还原旧 JSON 的“缺键”语义——否则
        scanner 中 `entry.get("retry_count", 0)` 会拿到 None 而非默认值并报错。

        Return the file's record as a dict, or None if absent.
        Important: NULL-valued columns are dropped so the dict matches the old
        JSON's "missing key" semantics—otherwise scanner's
        `entry.get("retry_count", 0)` would get None instead of the default.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE filepath=?", (filepath,)
            ).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys() if k != "filepath" and row[k] is not None}

    def _get_status(self, filepath):
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM files WHERE filepath=?", (filepath,)
            ).fetchone()
        return row["status"] if row else None

    def iter_started(self):
        """
        遍历所有 status == "started" 的文件路径（用于启动时清理残留状态）。
        先物化成列表再 yield——调用方（mark_stale_started_as_failed）会边遍历边写。
        Yield all filepaths whose status is "started" (for stale-state cleanup).
        Materialised into a list first—the caller writes while iterating.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT filepath FROM files WHERE status='started'"
            ).fetchall()
        for row in rows:
            yield row["filepath"]

    def is_rejected(self, filepath):
        return self._get_status(filepath) == "rejected"

    def is_success(self, filepath):
        return self._get_status(filepath) == "success"

    def is_not_required(self, filepath):
        return self._get_status(filepath) == "not required"

    def is_started(self, filepath):
        return self._get_status(filepath) == "started"

    def exists(self, filepath):
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM files WHERE filepath=? LIMIT 1", (filepath,)
            ).fetchone()
        return row is not None

    def mark_started(self, filepath):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filepath, status, started_at, completed_at, output_path, error) "
                "VALUES (?, 'started', ?, NULL, NULL, NULL)",
                (filepath, time.time()),
            )

    def mark_done(self, filepath, output_path, vmaf_score=None, rejected=False):
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM files WHERE filepath=?", (filepath,)
            ).fetchone()
            started_at = row["started_at"] if row and row["started_at"] is not None else time.time()
            self._conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filepath, status, started_at, completed_at, output_path, vmaf_score, error) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    filepath,
                    "rejected" if rejected else "success",
                    started_at,
                    time.time(),
                    None if rejected else output_path,
                    vmaf_score,
                ),
            )

    def mark_skipped(self, filepath, codec):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO files (filepath, status, codec, skipped_at) "
                "VALUES (?, 'not required', ?, ?)",
                (filepath, codec, time.time()),
            )

    def mark_ready(self, filepath, codec=None):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO files (filepath, status, codec) "
                "VALUES (?, 'ready', ?)",
                (filepath, codec),
            )

    def mark_exhausted(self, filepath):
        with self._lock:
            self._conn.execute(
                "UPDATE files SET status='rejected', error='exhausted_retries' WHERE filepath=?",
                (filepath,),
            )

    def mark_failed(self, filepath, error):
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at, retry_count FROM files WHERE filepath=?", (filepath,)
            ).fetchone()
            started_at = row["started_at"] if row and row["started_at"] is not None else time.time()
            old_retry = row["retry_count"] if row and row["retry_count"] is not None else -1
            self._conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filepath, status, started_at, completed_at, output_path, error, retry_count) "
                "VALUES (?, 'failed', ?, ?, NULL, ?, ?)",
                (filepath, started_at, time.time(), str(error), old_retry + 1),
            )
