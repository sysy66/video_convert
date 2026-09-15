"""
历史记录模块。使用 SQLite 持久化每个文件的转码状态（pending_transcode/
pending_postprocess/success/rejected/failed/not required 等），线程安全，
供扫描、转码与主循环使用。

History tracking module. Persists each file's transcode status
(pending_transcode/pending_postprocess/success/rejected/failed/not required,
etc.) in SQLite, is thread-safe, and is used by the scanner, transcoder and
main loop.
"""

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
        """初始化 SQLite 连接和表结构。Initialize SQLite and create the schema."""
        with self._lock:
            self._configure_connection()
            self._create_schema()

    def _configure_connection(self):
        """
        配置连接级 PRAGMA；调用方必须已经持有 _lock。
        Configure connection-level PRAGMAs; the caller must hold _lock.
        """
        # WAL：读不阻塞写；synchronous=NORMAL + WAL 在本地盘崩溃安全；busy_timeout 吸收锁等待。
        # WAL: readers don't block the writer; NORMAL+WAL is crash-safe on local
        # disk; busy_timeout absorbs any lock wait.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _create_schema(self):
        """
        创建当前版本的 files 表；不负责旧库迁移。
        Create the current files table; legacy database migration is out of scope.
        """
        self._conn.execute(
            """
            -- 当前状态 / Current states:
            -- pending_transcode | pending_postprocess | success |
            -- rejected | failed | not required
            CREATE TABLE IF NOT EXISTS files (
                filepath       TEXT PRIMARY KEY, -- 源文件绝对路径 / Absolute source path
                status         TEXT NOT NULL CHECK (
                    status IN (
                        'pending_transcode',
                        'pending_postprocess',
                        'success',
                        'rejected',
                        'failed',
                        'not required'
                    )
                ),
                transcode_started_at  REAL,      -- 转码开始 / Transcode start
                transcode_finished_at REAL,      -- 转码结束 / Transcode finish
                vmaf_started_at       REAL,      -- VMAF 开始 / VMAF start
                vmaf_finished_at      REAL,      -- VMAF 结束 / VMAF finish
                completed_at          REAL,      -- 写入终态 / Terminal-state time
                output_path    TEXT CHECK (
                    (
                        status IN ('success', 'pending_postprocess')
                        AND output_path IS NOT NULL
                    )
                    OR status = 'rejected'
                    OR (
                        status IN ('pending_transcode', 'failed', 'not required')
                        AND output_path IS NULL
                    )
                ),                              -- 已生成产物路径 / Materialized output path
                vmaf_score     REAL,             -- VMAF 分数 / VMAF score
                error          TEXT,             -- 错误信息 / Error message
                codec          TEXT,             -- 跳过时的源编码 / Source codec when skipped
                skipped_at     REAL              -- Unix 时间戳 / Unix timestamp
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
        关键：剔除值为 NULL 的列，忠实还原旧 JSON 的“缺键”语义。

        Return the file's record as a dict, or None if absent.
        Important: NULL-valued columns are dropped so the dict matches the old
        JSON's "missing key" semantics.
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE filepath=?", (filepath,)).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys() if k != "filepath" and row[k] is not None}

    def _get_status(self, filepath):
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM files WHERE filepath=?", (filepath,)
            ).fetchone()
        return row["status"] if row else None

    def _get_phase_times(self, filepath):
        """
        读取阶段时间戳；调用方必须已经持有 _lock。
        Read phase timestamps; callers must already hold _lock.
        """
        return self._conn.execute(
            "SELECT transcode_started_at, transcode_finished_at, "
            "vmaf_started_at, vmaf_finished_at FROM files WHERE filepath=?",
            (filepath,),
        ).fetchone()

    def iter_started(self):
        """
        遍历所有 status == "pending_transcode" 的文件路径（用于启动时重入队）。
        物化成列表返回，避免返回生成器后并发写者修改导致游标问题。

        Return all filepaths whose status is "pending_transcode"
        (re-enqueued on startup). Materialised into a list so the caller can
        iterate safely while the DB may be written to concurrently.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT filepath FROM files WHERE status='pending_transcode'"
            ).fetchall()
        return [r["filepath"] for r in rows]

    def iter_final_states(self):
        """
        遍历终态记录（rejected/failed/not required）的路径，供孤儿记录清理。
        排除 pending_transcode（转码线程收尾），避免与运行中的并发写者竞争。
        Yield filepaths of terminal-state records (rejected/failed/not required)
        for orphan cleanup. Excludes pending_transcode (finished by the transcode
        thread) to avoid races.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT filepath FROM files WHERE status IN ('rejected', 'failed', 'not required')"
            ).fetchall()
        for row in rows:
            yield row["filepath"]

    def delete(self, filepath):
        """
        删除该文件的记录（用于源文件已不存在的孤儿记录清理）。
        Delete the record for a file (used when cleaning up orphan records whose
        source file no longer exists).
        """
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE filepath=?", (filepath,))

    def is_rejected(self, filepath):
        return self._get_status(filepath) == "rejected"

    def is_success(self, filepath):
        return self._get_status(filepath) == "success"

    def is_not_required(self, filepath):
        return self._get_status(filepath) == "not required"

    def is_started(self, filepath):
        """True iff status == "pending_transcode"."""
        return self._get_status(filepath) == "pending_transcode"

    def exists(self, filepath):
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM files WHERE filepath=? LIMIT 1", (filepath,)
            ).fetchone()
        return row is not None

    def mark_pending_transcode(self, filepath):
        """
        标记一次新的转码尝试；清空上一轮阶段时间和终态信息。
        此时还没有成功产物，因此 output_path 必须保持 NULL。
        Start a new transcode attempt and clear the previous phase/terminal data.
        No successful output exists yet, so output_path remains NULL.
        """
        with self._lock:
            transcode_started_at = time.time()
            self._conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filepath, status, transcode_started_at, transcode_finished_at, "
                "vmaf_started_at, vmaf_finished_at, completed_at, output_path, "
                "vmaf_score, error, codec, skipped_at) "
                "VALUES (?, 'pending_transcode', ?, NULL, NULL, NULL, NULL, NULL, "
                "NULL, NULL, NULL, NULL)",
                (filepath, transcode_started_at),
            )

    def mark_transcode_finished(self, filepath):
        """
        记录转码产物已经生成；不改变当前状态。
        Record that the transcode output has been generated without changing status.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE files SET transcode_finished_at=? WHERE filepath=?",
                (time.time(), filepath),
            )

    def mark_pending_postprocess(self, filepath, output_path):
        """
        标记文件进入后处理认领：status = "pending_postprocess"，并把 output_path
        指向已落盘产物。仅 async 落盘后调用（不变量：此状态 ⟺ 已落盘）。
        Transition a file to post-process claimed: status = "pending_postprocess"
        with output_path pointing at the landed file. Called only after the async
        early landing (invariant: this status implies already landed).
        """
        with self._lock:
            row = self._get_phase_times(filepath)
            now = time.time()
            transcode_started_at = (
                row["transcode_started_at"]
                if row and row["transcode_started_at"] is not None
                else now
            )
            transcode_finished_at = (
                row["transcode_finished_at"]
                if row and row["transcode_finished_at"] is not None
                else now
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filepath, status, transcode_started_at, transcode_finished_at, "
                "vmaf_started_at, vmaf_finished_at, completed_at, output_path, "
                "vmaf_score, error, codec, skipped_at) "
                "VALUES (?, 'pending_postprocess', ?, ?, NULL, NULL, NULL, ?, "
                "NULL, NULL, NULL, NULL)",
                (filepath, transcode_started_at, transcode_finished_at, str(output_path)),
            )

    def mark_vmaf_started(self, filepath):
        """
        记录一次 VMAF 尝试开始，并清除该尝试的结束时间。
        Record the start of a VMAF attempt and clear its finish time.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE files SET vmaf_started_at=?, vmaf_finished_at=NULL WHERE filepath=?",
                (time.time(), filepath),
            )

    def mark_vmaf_finished(self, filepath):
        """
        记录 VMAF 调用返回；分数是否可用由 vmaf_score 单独表示。
        Record that the VMAF call returned; score availability is represented by
        vmaf_score separately.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE files SET vmaf_finished_at=? WHERE filepath=?",
                (time.time(), filepath),
            )

    def iter_pending_postprocess(self):
        """
        遍历 status == "pending_postprocess" 的文件路径（启动 recover 用）。
        Yield filepaths with status == "pending_postprocess" (startup recovery).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT filepath FROM files WHERE status='pending_postprocess'"
            ).fetchall()
        return [r["filepath"] for r in rows]

    def mark_done(self, filepath, output_path, vmaf_score=None, rejected=False):
        """
        记录终态；output_path 只保存实际存在的转码产物路径。
        Record a terminal state; output_path stores an actual transcoded output
        whenever one was retained.
        """
        with self._lock:
            row = self._get_phase_times(filepath)
            now = time.time()
            transcode_started_at = (
                row["transcode_started_at"]
                if row and row["transcode_started_at"] is not None
                else now
            )
            transcode_finished_at = (
                row["transcode_finished_at"]
                if row and row["transcode_finished_at"] is not None
                else now
            )
            vmaf_started_at = row["vmaf_started_at"] if row else None
            vmaf_finished_at = row["vmaf_finished_at"] if row else None
            stored_output = str(output_path) if output_path is not None else None
            self._conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filepath, status, transcode_started_at, transcode_finished_at, "
                "vmaf_started_at, vmaf_finished_at, completed_at, output_path, "
                "vmaf_score, error, codec, skipped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                (
                    filepath,
                    "rejected" if rejected else "success",
                    transcode_started_at,
                    transcode_finished_at,
                    vmaf_started_at,
                    vmaf_finished_at,
                    now,
                    stored_output,
                    vmaf_score,
                ),
            )

    def mark_skipped(self, filepath, codec):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filepath, status, transcode_started_at, transcode_finished_at, "
                "vmaf_started_at, vmaf_finished_at, completed_at, output_path, "
                "vmaf_score, error, codec, skipped_at) "
                "VALUES (?, 'not required', NULL, NULL, NULL, NULL, NULL, NULL, "
                "NULL, NULL, ?, ?)",
                (filepath, codec, time.time()),
            )

    def mark_failed(self, filepath, error):
        with self._lock:
            row = self._get_phase_times(filepath)
            now = time.time()
            transcode_started_at = (
                row["transcode_started_at"]
                if row and row["transcode_started_at"] is not None
                else now
            )
            transcode_finished_at = row["transcode_finished_at"] if row else None
            vmaf_started_at = row["vmaf_started_at"] if row else None
            vmaf_finished_at = row["vmaf_finished_at"] if row else None
            self._conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filepath, status, transcode_started_at, transcode_finished_at, "
                "vmaf_started_at, vmaf_finished_at, completed_at, output_path, "
                "vmaf_score, error, codec, skipped_at) "
                "VALUES (?, 'failed', ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL)",
                (
                    filepath,
                    transcode_started_at,
                    transcode_finished_at,
                    vmaf_started_at,
                    vmaf_finished_at,
                    now,
                    str(error),
                ),
            )

    def iter_failed(self):
        with self._lock:
            rows = self._conn.execute("SELECT filepath FROM files WHERE status='failed'").fetchall()
        return [r["filepath"] for r in rows]

    def purge_orphan_records(self):
        """清理源文件已不存在的终态记录。

        success 记录的源缺失是 ACCEPT 删源后的正常状态（用于同名去重），永不清理；
        pending_transcode 由转码线程收尾。仅处理源文件
        已消失的 rejected/failed/not required 记录——记录失去意义（无文件要重试或保护）。

        Purge terminal records whose source file no longer exists. A missing source on a
        success record is the normal post-ACCEPT state (used for same-name dedup) and is
        never removed; pending_transcode records are finalized by the transcode
        threads. Only rejected/failed/not-required records whose source is gone
        are removed, since they no longer serve a purpose (nothing to retry or protect).
        """
        for filepath in self.iter_final_states():
            if not os.path.exists(filepath):
                log.debug("PURGE (orphan record, file missing): %s", filepath)
                self.delete(filepath)

    def get_output_path(self, filepath):
        """
        获取记录中的已生成/已落盘产物路径；转码进行中的临时路径不在此字段中保存。
        Retrieve the materialized output path; temp paths for active transcoding are
        not stored in this field.

        :param filepath: 源文件路径 (Source file path)
        :return: 产物路径，无记录时返回 None (Output path, or None if no record exists)
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT output_path FROM files WHERE filepath=?", (filepath,)
            ).fetchone()
        return row["output_path"] if row else None
