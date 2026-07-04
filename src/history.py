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
            return {"files": {}, "last_full_scan": 0}
        try:
            with open(self._path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error("Failed to load history: %s", e)
            return {"files": {}, "last_full_scan": 0}

    def save(self):
        with self._lock:
            tmp = self._path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp, self._path)
            except OSError as e:
                log.error("Failed to save history: %s", e)

    def iter_started(self):
        """
        遍历所有 status == "started" 的文件路径（用于启动时清理残留状态）。
        Yield all filepaths whose status is "started" (for stale-state cleanup on startup).
        """
        with self._lock:
            for fp, entry in list(self._data["files"].items()):
                if entry.get("status") == "started":
                    yield fp

    def is_done(self, filepath):
        entry = self._data["files"].get(filepath)
        return entry is not None and entry.get("status") == "done"

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
                "status": "done",
                "started_at": self._data["files"].get(filepath, {}).get("started_at", time.time()),
                "completed_at": time.time(),
                "output_path": output_path,
                "vmaf_score": vmaf_score,
                "rejected": rejected,
                "error": None,
            }
            self.save()

    def mark_failed(self, filepath, error, sw_fallback=False):
        with self._lock:
            entry = {
                "status": "failed",
                "started_at": self._data["files"].get(filepath, {}).get("started_at", time.time()),
                "completed_at": time.time(),
                "output_path": None,
                "error": str(error),
            }
            if sw_fallback:
                entry["sw_fallback"] = True
            self._data["files"][filepath] = entry
            self.save()

    def get_fallback(self, filepath):
        with self._lock:
            entry = self._data["files"].get(filepath)
            return bool(entry and entry.get("sw_fallback"))

    def get_last_full_scan(self):
        return self._data.get("last_full_scan", 0)

    def set_last_full_scan(self, timestamp):
        with self._lock:
            self._data["last_full_scan"] = timestamp
            self.save()
