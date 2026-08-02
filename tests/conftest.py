import pytest

from src.history import History


@pytest.fixture
def make_history(tmp_path):
    def _make_history(name="history.db"):
        return History(str(tmp_path / name))

    return _make_history


@pytest.fixture
def make_video_file(tmp_path):
    def _make_video_file(name="test.mp4"):
        f = tmp_path / name
        f.write_text("fake video content")
        return f

    return _make_video_file


@pytest.fixture
def make_video_tree(tmp_path):
    def _make_video_tree(structure):
        for path, codec in structure.items():
            p = tmp_path / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(codec if codec else "fake content")
        return tmp_path

    return _make_video_tree
