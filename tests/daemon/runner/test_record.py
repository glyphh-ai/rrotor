import json

from rrotord.runner import record
from rrotord.runner.record import Records, Run, new_id


def run(**kw) -> Run:
    return Run(new_id(), "p", 1, "a" * 40, "start", {"secret": "x"}, "claude", "local", **kw)


def test_final_statuses():
    assert run(status="done").final and not run().final and not run(status="needs_input").final


def test_view_leaves_out_the_inputs():
    view = run(seconds=1.23456).view()
    assert "inputs" not in view and view["seconds"] == 1.235 and view["status"] == "running"


def test_save_and_load(tmp_path):
    records, r = Records(tmp_path), run()
    records.save(r)
    assert records.load(r.id) == r


def test_a_run_an_earlier_release_saved_names_its_program_model_and_still_loads(tmp_path):
    records, r = Records(tmp_path), run()
    records.save(r)
    path = records.folder(r.id) / "run.json"
    saved = json.loads(path.read_text())
    saved["model"] = saved.pop("program")
    path.write_text(json.dumps(saved))
    assert records.load(r.id) == r and records.all() == [r]


def test_load_refuses_what_is_not_a_run_id(tmp_path):
    assert Records(tmp_path).load("../etc") is None


def test_load_of_a_run_not_here(tmp_path):
    assert Records(tmp_path).load(new_id()) is None


def test_all_in_creation_order(tmp_path):
    records = Records(tmp_path)
    assert records.all() == []
    a, b = run(created_at="2026-01-02"), run(created_at="2026-01-01")
    records.save(a)
    records.save(b)
    (tmp_path / "runs" / "junk").mkdir()
    assert [r.id for r in records.all()] == [b.id, a.id]


def test_log_lines_are_cut_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(record, "LOG_LINE_MAX", 5)
    records = Records(tmp_path)
    records.log("r", {"message": "123456789", "line": "ok", "n": 1})
    records.log("r", {"line": "abcdefgh"})
    lines = records.lines("r")
    assert lines[0]["message"] == "12345…" and lines[0]["line"] == "ok" and lines[1]["line"] == "abcde…"
    assert records.lines("r", limit=1) == lines[1:]


def test_in_a_cluster_a_run_id_names_the_node_that_holds_it(tmp_path):
    from rrotord.runner.record import holder

    made = new_id("rrotor-1")
    assert made.startswith("rrotor-1~") and holder(made) == "rrotor-1" and holder(new_id()) is None
    records = Records(tmp_path)
    r = Run(made, "p", 1, "a" * 40, "start", {}, "claude", "local")
    records.save(r)
    assert records.load(made) == r
    assert records.load("Bad Node~" + made.split("~")[1]) is None
    assert records.load("rrotor-1~not-a-uuid") is None
