import sqlite3

from src.history import History


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
    def test_new_db_returns_none(self, tmp_path):
        h = History(str(tmp_path / "history.db"))
        assert h.get_entry("/x") is None

    def test_creates_db_file(self, tmp_path):
        p = str(tmp_path / "history.db")
        History(p)
        assert (tmp_path / "history.db").exists()


class TestHistoryQueries:
    def test_is_success_unknown(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        assert h.is_success("/x") is False

    def test_exists_unknown(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        assert h.exists("/x") is False

    def test_get_entry_drops_null_columns(self, tmp_path):
        # not required 记录只写 status/codec/skipped_at，其余列应从返回字典中剔除。
        # A "not required" record only sets status/codec/skipped_at; other columns
        # must be absent from the returned dict (not present-with-None).
        h = History(str(tmp_path / "h.db"))
        h.mark_skipped("/v.mp4", "hevc")
        entry = h.get_entry("/v.mp4")
        assert entry["status"] == "not required"
        assert entry["codec"] == "hevc"
        assert "completed_at" not in entry
        assert "retry_count" not in entry


class TestHistoryMarks:
    def test_mark_started(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_started("/v.mp4")
        assert h.exists("/v.mp4")
        assert h.is_started("/v.mp4")
        assert h.is_success("/v.mp4") is False
        assert h.get_entry("/v.mp4")["status"] == "started"

    def test_mark_done(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_done("/v.mp4", "/v.mkv")
        assert h.is_success("/v.mp4")
        entry = h.get_entry("/v.mp4")
        assert entry["output_path"] == "/v.mkv"
        assert entry["completed_at"] is not None

    def test_mark_done_rejected(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_done("/v.mp4", None, vmaf_score=65.0, rejected=True)
        assert h.is_rejected("/v.mp4")
        assert h.is_success("/v.mp4") is False
        entry = h.get_entry("/v.mp4")
        assert entry["vmaf_score"] == 65.0
        assert entry["completed_at"] is not None
        # 拒绝时不保留输出路径。Rejected keeps no output path.
        assert "output_path" not in entry

    def test_mark_skipped(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_skipped("/v.mp4", "av1")
        assert h.is_not_required("/v.mp4")
        assert h.get_entry("/v.mp4")["codec"] == "av1"

    def test_mark_failed(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_failed("/v.mp4", "some error")
        assert h.exists("/v.mp4")
        assert h.is_success("/v.mp4") is False
        entry = h.get_entry("/v.mp4")
        assert entry["status"] == "failed"
        assert entry["error"] == "some error"

    def test_mark_failed_increments_retry_count(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_failed("/v.mp4", "err1")
        assert h.get_entry("/v.mp4")["retry_count"] == 0
        h.mark_failed("/v.mp4", "err2")
        assert h.get_entry("/v.mp4")["retry_count"] == 1

    def test_mark_started_then_done_preserves_started_at(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_started("/v.mp4")
        started = h.get_entry("/v.mp4")["started_at"]
        h.mark_done("/v.mp4", "/v.mkv")
        assert h.get_entry("/v.mp4")["started_at"] == started

    def test_mark_started_then_failed_preserves_started_at(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_started("/v.mp4")
        started = h.get_entry("/v.mp4")["started_at"]
        h.mark_failed("/v.mp4", "err")
        assert h.get_entry("/v.mp4")["started_at"] == started


class TestHistoryIter:
    def test_iter_started(self, tmp_path):
        h = History(str(tmp_path / "h.db"))
        h.mark_started("/a.mp4")
        h.mark_started("/b.mp4")
        h.mark_done("/c.mp4", "/c.mkv")
        assert set(h.iter_started()) == {"/a.mp4", "/b.mp4"}


class TestHistoryPersistence:
    def test_data_survives_reload(self, tmp_path):
        p = str(tmp_path / "h.db")
        h1 = History(p)
        h1.mark_done("/a.mp4", "/a.mkv")

        h2 = History(p)
        assert h2.is_success("/a.mp4")

    def test_written_to_disk(self, tmp_path):
        p = str(tmp_path / "h.db")
        h = History(p)
        h.mark_done("/v.mp4", "/v.mkv")
        assert _db_row(p, "/v.mp4")["status"] == "success"
