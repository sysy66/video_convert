import json
import time
from src.history import History


class TestHistoryLoad:
    def test_new_file_returns_empty(self, tmp_path):
        h = History(str(tmp_path / "history.json"))
        assert h._data["files"] == {}
        assert h.get_last_full_scan() == 0

    def test_load_existing_file(self, tmp_path):
        p = str(tmp_path / "history.json")
        with open(p, "w") as f:
            json.dump({"files": {"/a.mp4": {"status": "done"}}, "last_full_scan": 100}, f)
        h = History(p)
        assert h.is_done("/a.mp4")
        assert h.get_last_full_scan() == 100

    def test_corrupted_file_returns_empty(self, tmp_path):
        p = str(tmp_path / "history.json")
        with open(p, "w") as f:
            f.write("not json")
        h = History(p)
        assert h._data["files"] == {}
        assert h.get_last_full_scan() == 0


class TestHistoryQueries:
    def test_is_done_unknown(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        assert h.is_done("/x") is False

    def test_exists_unknown(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        assert h.exists("/x") is False


class TestHistoryMarks:
    def test_mark_started(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        h.mark_started("/v.mp4")
        assert h.exists("/v.mp4")
        assert h.is_done("/v.mp4") is False
        assert h._data["files"]["/v.mp4"]["status"] == "started"

    def test_mark_done(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        h.mark_done("/v.mp4", "/v.mkv")
        assert h.is_done("/v.mp4")
        assert h._data["files"]["/v.mp4"]["output_path"] == "/v.mkv"
        assert h._data["files"]["/v.mp4"]["completed_at"] is not None

    def test_mark_failed(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        h.mark_failed("/v.mp4", "some error")
        assert h.exists("/v.mp4")
        assert h.is_done("/v.mp4") is False
        assert h._data["files"]["/v.mp4"]["status"] == "failed"
        assert h._data["files"]["/v.mp4"]["error"] == "some error"

    def test_mark_started_then_done_preserves_started_at(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        h.mark_started("/v.mp4")
        started = h._data["files"]["/v.mp4"]["started_at"]
        h.mark_done("/v.mp4", "/v.mkv")
        assert h._data["files"]["/v.mp4"]["started_at"] == started

    def test_mark_started_then_failed_preserves_started_at(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        h.mark_started("/v.mp4")
        started = h._data["files"]["/v.mp4"]["started_at"]
        h.mark_failed("/v.mp4", "err")
        assert h._data["files"]["/v.mp4"]["started_at"] == started


class TestHistoryPersistence:
    def test_data_survives_reload(self, tmp_path):
        p = str(tmp_path / "h.json")
        h1 = History(p)
        h1.mark_done("/a.mp4", "/a.mkv")
        h1.set_last_full_scan(999)

        h2 = History(p)
        assert h2.is_done("/a.mp4")
        assert h2.get_last_full_scan() == 999

    def test_file_written_to_disk(self, tmp_path):
        p = str(tmp_path / "h.json")
        h = History(p)
        h.mark_done("/v.mp4", "/v.mkv")
        with open(p) as f:
            data = json.load(f)
        assert data["files"]["/v.mp4"]["status"] == "done"


class TestHistoryFullScan:
    def test_default_is_zero(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        assert h.get_last_full_scan() == 0

    def test_set_and_get(self, tmp_path):
        h = History(str(tmp_path / "h.json"))
        h.set_last_full_scan(12345)
        assert h.get_last_full_scan() == 12345
