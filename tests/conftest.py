from src.history import History


def make_history(tmp_path):
    path = str(tmp_path / "history.db")
    return History(path)


def make_video_file(tmp_path, name="test.mp4"):
    f = tmp_path / name
    f.write_text("fake video content")
    return f


def make_video_tree(tmp_path, structure):
    for path, codec in structure.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(codec if codec else "fake content")
