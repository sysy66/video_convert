import json
import os
import threading
import time

log = __import__("logging").getLogger(__name__)


class History:
    def __init__(self, path):
        self._path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return {"files": {}}
        try:
            with open(self._path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error("Failed to load history: %s", e)
            return {"files": {}}

    def save(self):
        with self._lock:
            tmp = self._path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp, self._path)
            except OSError as e:
                log.error("Failed to save history: %s", e)

    def get_entry(self, filepath):
        return self._data["files"].get(filepath)

    def iter_started(self):
        """
        遍历所有 status == "started" 的文件路径（用于启动时清理残留状态）。
        Yield all filepaths whose status is "started" (for stale-state cleanup on startup).
        """
        with self._lock:
            for fp, entry in list(self._data["files"].items()):
                if entry.get("status") == "started":
                    yield fp

    def is_rejected(self, filepath):
        entry = self._data["files"].get(filepath)
        return entry is not None and entry.get("status") == "rejected"

    def is_success(self, filepath):
        entry = self._data["files"].get(filepath)
        return entry is not None and entry.get("status") == "success"

    def is_not_required(self, filepath):
        entry = self._data["files"].get(filepath)
        return entry is not None and entry.get("status") == "not required"

    def is_started(self, filepath):
        entry = self._data["files"].get(filepath)
        return entry is not None and entry.get("status") == "started"

    def exists(self, filepath):
        return filepath in self._data["files"]

    def mark_started(self, filepath):
        with self._lock:
            self._data["files"][filepath] = {
                "status": "started",
                "started_at": time.time(),
                "completed_at": None,
                "output_path": None,
                "error": None
            }
            self.save()

    def mark_done(self, filepath, output_path, vmaf_score=None, rejected=False):
        with self._lock:
            self._data["files"][filepath] = {
                "status": "rejected" if rejected else "success",
                "started_at": self._data["files"].get(filepath, {}).get("started_at", time.time()),
                "completed_at": time.time(),
                "output_path": None if rejected else output_path,
                "vmaf_score": vmaf_score,
                "error": None,
            }
            self.save()

    def mark_skipped(self, filepath, codec):
        with self._lock:
            self._data["files"][filepath] = {
                "status": "not required",
                "codec": codec,
                "skipped_at": time.time(),
            }
            self.save()

    def get_cached_codec(self, filepath):
        entry = self._data["files"].get(filepath)
        if entry and entry.get("codec"):
            return entry["codec"]
        return None

    def mark_failed(self, filepath, error):
        with self._lock:
            old = self._data["files"].get(filepath, {})
            retry_count = old.get("retry_count", -1) + 1
            self._data["files"][filepath] = {
                "status": "failed",
                "started_at": old.get("started_at", time.time()),
                "completed_at": time.time(),
                "output_path": None,
                "error": str(error),
                "retry_count": retry_count,
            }
            self.save()
