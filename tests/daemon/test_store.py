import stat

import pytest

from rrotord import store


def test_write_and_read_json(tmp_path):
    path = tmp_path / "a" / "b.json"
    store.write_json(path, {"x": 1})
    assert store.read_json(path) == {"x": 1}
    assert list(path.parent.iterdir()) == [path]


def test_private_files_are_0600(tmp_path):
    path = tmp_path / "s.json"
    store.write_json(path, {}, private=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_failed_write_leaves_nothing_behind(tmp_path):
    with pytest.raises(TypeError):
        store.write_json(tmp_path / "x.json", {"x": object()})
    assert list(tmp_path.iterdir()) == []


def test_read_json_of_a_missing_file_is_the_default(tmp_path):
    assert store.read_json(tmp_path / "none", 5) == 5


def test_lines_append_and_read_back_the_last(tmp_path):
    path = tmp_path / "l" / "log.jsonl"
    for i in range(3):
        store.append_line(path, {"i": i})
    path.write_text(path.read_text() + "\n")
    assert store.read_lines(path) == [{"i": 0}, {"i": 1}, {"i": 2}]
    assert store.read_lines(path, 2) == [{"i": 2}]  # the blank line counts among the last two


def test_read_lines_of_a_missing_file_is_empty(tmp_path):
    assert store.read_lines(tmp_path / "none") == []
