import tempfile
import unittest
from pathlib import Path

from src.walker import walk_directory


class TestWalkerBasic(unittest.TestCase):
    @staticmethod
    def _make_tree(root, files):
        for f in files:
            p = root / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("content")

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            result = list(walk_directory(d, {".mp4"}))
            self.assertEqual(result, [])

    def test_nonexistent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            result = list(walk_directory(d + "/nonexistent", {".mp4"}))
            self.assertEqual(result, [])

    def test_extension_filtering(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_tree(root, {"a.mp4": "", "b.mkv": "", "c.txt": "", "d.jpg": ""})
            result = list(walk_directory(d, {".mp4", ".mkv"}))
            self.assertEqual(len(result), 2)
            names = {p.name for p in result}
            self.assertEqual(names, {"a.mp4", "b.mkv"})

    def test_single_extension(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_tree(root, {"a.mp4": "", "b.mkv": ""})
            result = list(walk_directory(d, {".mp4"}))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "a.mp4")


class TestWalkerHidden(unittest.TestCase):
    @staticmethod
    def _make_tree(root, files):
        for f in files:
            p = root / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("content")

    def test_skips_hidden_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_tree(root, {".hidden.mp4": "", "normal.mp4": ""})
            result = list(walk_directory(d, {".mp4"}))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "normal.mp4")

    def test_skips_hidden_directories(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_tree(root, {".hidden/v.mp4": "", "visible/v.mp4": ""})
            result = list(walk_directory(d, {".mp4"}))
            self.assertEqual(len(result), 1)
            self.assertIn("visible", str(result[0]))

    def test_skips_temp_directories(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_tree(root, {".work_abc/v.mp4": "", "normal/v.mp4": ""})
            result = list(walk_directory(d, {".mp4"}))
            self.assertEqual(len(result), 1)
            self.assertIn("normal", str(result[0]))


class TestWalkerRecursive(unittest.TestCase):
    @staticmethod
    def _make_tree(root, files):
        for f in files:
            p = root / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("content")

    def test_recursive_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._make_tree(root, {"a.mp4": "", "sub/b.mp4": "", "sub/sub/c.mp4": ""})
            result = list(walk_directory(d, {".mp4"}))
            self.assertEqual(len(result), 3)


if __name__ == "__main__":
    unittest.main()
