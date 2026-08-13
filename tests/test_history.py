"""history 模块单元测试：SQLite 历史状态记录与查询。

Unit tests for the history module: SQLite state tracking and queries.
"""

import sqlite3

import pytest

pytestmark = pytest.mark.unit


def _db_row(db_path, filepath):
    """直接读 SQLite 验证已落盘的行。Read a persisted row straight from SQLite."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM files WHERE filepath=?", (filepath,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


class TestHistoryInit:
    def test_new_db_returns_none(self, make_history):
        h = make_history()
        assert h.get_entry("/x") is None

    def test_creates_db_file(self, tmp_path, make_history):
        make_history()
        assert (tmp_path / "history.db").exists()

    def test_legacy_db_migrates_output_frames_column(self, tmp_path, make_history):
        # 老库（无 output_frames 列）打开后自动 ALTER 补列，旧数据不丢。
        # A legacy DB without output_frames must gain the column on open
        # without losing existing rows.
        conn = sqlite3.connect(str(tmp_path / "legacy.db"))
        conn.execute(
            "CREATE TABLE files ("
            "filepath TEXT PRIMARY KEY, status TEXT NOT NULL, started_at REAL, "
            "completed_at REAL, output_path TEXT, vmaf_score REAL, error TEXT, "
            "retry_count INTEGER, codec TEXT, skipped_at REAL)"
        )
        conn.execute(
            "INSERT INTO files (filepath, status) VALUES ('/old.mp4', 'await_postprocess')"
        )
        conn.commit()
        conn.close()

        h = make_history("legacy.db")
        cols = {r["name"] for r in h._conn.execute("PRAGMA table_info(files)").fetchall()}
        assert "output_frames" in cols
        assert h.get_entry("/old.mp4")["status"] == "await_postprocess"

        # 新方法写帧数可持久化，iter 返回三元组
        h.mark_pending_vmaf("/old.mp4", "/old.mkv", 12345)
        h.close()
        h2 = make_history("legacy.db")
        assert h2.iter_pending_vmaf() == [("/old.mp4", "/old.mkv", 12345)]


class TestHistoryQueries:
    def test_is_success_unknown(self, make_history):
        h = make_history()
        assert h.is_success("/x") is False

    def test_exists_unknown(self, make_history):
        h = make_history()
        assert h.exists("/x") is False

    def test_get_entry_drops_null_columns(self, make_history):
        # not required 记录只写 status/codec/skipped_at，其余列应从返回字典中剔除。
        # A "not required" record only sets status/codec/skipped_at; other columns
        # must be absent from the returned dict (not present-with-None).
        h = make_history()
        h.mark_skipped("/v.mp4", "hevc")
        entry = h.get_entry("/v.mp4")
        assert entry["status"] == "not required"
        assert entry["codec"] == "hevc"
        assert "completed_at" not in entry
        assert "retry_count" not in entry


class TestHistoryMarks:
    def test_mark_started(self, make_history):
        h = make_history()
        h.mark_started("/v.mp4")
        assert h.exists("/v.mp4")
        assert h.is_started("/v.mp4")
        assert h.is_success("/v.mp4") is False
        assert h.get_entry("/v.mp4")["status"] == "pending_transcode"

    def test_mark_done(self, make_history):
        h = make_history()
        h.mark_done("/v.mp4", "/v.mkv")
        assert h.is_success("/v.mp4")
        entry = h.get_entry("/v.mp4")
        assert entry["output_path"] == "/v.mkv"
        assert entry["completed_at"] is not None

    def test_mark_done_rejected(self, make_history):
        h = make_history()
        h.mark_done("/v.mp4", None, vmaf_score=65.0, rejected=True)
        assert h.is_rejected("/v.mp4")
        assert h.is_success("/v.mp4") is False
        entry = h.get_entry("/v.mp4")
        assert entry["vmaf_score"] == 65.0
        assert entry["completed_at"] is not None
        # 拒绝时不保留输出路径。Rejected keeps no output path.
        assert "output_path" not in entry

    def test_mark_skipped(self, make_history):
        h = make_history()
        h.mark_skipped("/v.mp4", "av1")
        assert h.is_not_required("/v.mp4")
        assert h.get_entry("/v.mp4")["codec"] == "av1"

    def test_mark_failed(self, make_history):
        h = make_history()
        h.mark_failed("/v.mp4", "some error")
        assert h.exists("/v.mp4")
        assert h.is_success("/v.mp4") is False
        entry = h.get_entry("/v.mp4")
        assert entry["status"] == "failed"
        assert entry["error"] == "some error"

    def test_iter_failed_yields_paths(self, make_history):
        h = make_history()
        h.mark_failed("/a.mp4", "err")
        h.mark_failed("/b.mp4", "err")
        h.mark_started("/c.mp4")
        h.mark_skipped("/d.mp4", "hevc")
        assert h.iter_failed() == ["/a.mp4", "/b.mp4"]

    def test_mark_started_then_done_preserves_started_at(self, make_history):
        h = make_history()
        h.mark_started("/v.mp4")
        started = h.get_entry("/v.mp4")["started_at"]
        h.mark_done("/v.mp4", "/v.mkv")
        assert h.get_entry("/v.mp4")["started_at"] == started

    def test_mark_started_then_failed_preserves_started_at(self, make_history):
        h = make_history()
        h.mark_started("/v.mp4")
        started = h.get_entry("/v.mp4")["started_at"]
        h.mark_failed("/v.mp4", "err")
        assert h.get_entry("/v.mp4")["started_at"] == started


class TestHistoryIter:
    def test_iter_started(self, make_history):
        h = make_history()
        h.mark_started("/a.mp4")
        h.mark_started("/b.mp4")
        h.mark_done("/c.mp4", "/c.mkv")
        assert set(h.iter_started()) == {"/a.mp4", "/b.mp4"}

    def test_iter_pending_vmaf_yields_output_frames(self, make_history):
        h = make_history()
        h.mark_pending_vmaf("/p.mp4", "/p.mkv", 216315)
        h.mark_pending_vmaf("/q.mp4", "/q.mkv", None)
        assert h.iter_pending_vmaf() == [
            ("/p.mp4", "/p.mkv", 216315),
            ("/q.mp4", "/q.mkv", None),
        ]

    def test_iter_final_states(self, make_history):
        h = make_history()
        h.mark_done("/rej.mp4", None, rejected=True)
        h.mark_failed("/fail.mp4", "err")
        h.mark_skipped("/skip.mp4", "hevc")
        h.mark_done("/ok.mp4", "/ok.mkv")
        h.mark_started("/start.mp4")
        h.mark_pending_vmaf("/pend.mp4", "/pend.mkv")
        assert set(h.iter_final_states()) == {"/rej.mp4", "/fail.mp4", "/skip.mp4"}

    def test_delete_removes_record(self, make_history):
        h = make_history()
        h.mark_failed("/v.mp4", "err")
        assert h.exists("/v.mp4")
        h.delete("/v.mp4")
        assert not h.exists("/v.mp4")
        assert h.get_entry("/v.mp4") is None


class TestHistoryPersistence:
    def test_data_survives_reload(self, tmp_path, make_history):
        h1 = make_history("h.db")
        h1.mark_done("/a.mp4", "/a.mkv")

        h2 = make_history("h.db")
        assert h2.is_success("/a.mp4")

    def test_written_to_disk(self, tmp_path, make_history):
        h = make_history("h.db")
        h.mark_done("/v.mp4", "/v.mkv")
        assert _db_row(str(tmp_path / "h.db"), "/v.mp4")["status"] == "success"
