"""`rrotord cluster join | leave <node> | promote <node>`: on a node, authenticated by the cluster secret, never over
the API."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from rrotord import __main__ as entry
from rrotord.cluster import command
from tests.daemon.cluster.conftest import SECRET, Nodes, b64, settled, until
from tests.daemon.test_main import keep_logging


async def test_leave_and_join_by_command(nodes: Nodes):
    ids = ["a", "b"]
    a, b = nodes.make("a", ids), nodes.make("b", ids)
    await a.start()
    await b.start()
    # b's join committed at the head too: leave waits only CHANGE_WITHIN for the last membership change, then is busy
    await until(lambda: b.status == "ready" and settled(a, [b]) and a.raft.ready_for_change())
    code, said = await command.run(b.config, ["leave", "b"])  # on the follower: passed to the head
    assert code == 0, said
    assert json.loads(said) == {"removed": "b", "members": 1}
    await until(lambda: b.status == "refused")
    code, said = await command.run(b.config, ["join"])
    assert code == 0 and json.loads(said)["status"] == "ready"
    code, said = await command.run(a.config, ["leave", "nobody"])
    assert code == 1 and said.startswith("rrotord cluster leave: E_CLUSTER_MEMBER:")


async def test_what_the_command_line_refuses(nodes: Nodes):
    a = nodes.make("a", ["a"])
    for wrong in ([], ["leave"], ["join", "x"], ["dance"], ["promote"], ["promote", "a", "--soon"]):
        assert await command.run(a.config, wrong) == (2, f"usage: {command.USAGE}")
    code, said = await command.run(replace(a.config, cluster=False), ["join"])
    assert code == 2 and "E_CONFIG" in said
    code, said = await command.run(a.config, ["join"])  # nothing listens: the node is not running
    assert code == 1 and "E_CLUSTER_UNREACHABLE" in said


@pytest.fixture
def cluster_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name, value in {
        "RROTOR_MODE": "self",
        "RROTOR_RUNTIME_ID": "6f1c2a9e-0000-4000-8000-000000000001",
        "RROTOR_RUNTIME_TOKEN": "tok",
        "RROTOR_PUBLIC_HOST": "rrotor.example.com",
        "RROTOR_CLUSTER": "on",
        "RROTOR_CLUSTER_PEERS": "127.0.0.1:1",
        "RROTOR_CLUSTER_PORT": "1",
        "RROTOR_CLUSTER_SECRET": b64(SECRET),
        "RROTOR_RUN_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)


def test_rrotord_cluster_runs_the_command_and_exits_with_its_code(cluster_env, monkeypatch, capsys):
    asked = []

    async def run(settings, args, confirm):
        asked.append((settings.cluster, args, confirm))
        return (0, "{}") if args == ["join"] else (1, "rrotord cluster leave: E_X: no")

    monkeypatch.setattr(command, "run", run)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert keep_logging(lambda: entry.main(["cluster", "join"])) == 0
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)  # at a terminal: the operator is asked
    assert keep_logging(lambda: entry.main(["cluster", "leave", "b"])) == 1
    out = capsys.readouterr()
    assert asked == [(True, ["join"], None), (True, ["leave", "b"], command.terminal)]
    assert out.out == "{}\n" and out.err == "rrotord cluster leave: E_X: no\n"
    assert keep_logging(lambda: entry.main(["serve"])) == 2
    assert f"usage: rrotord, or {command.USAGE}" in capsys.readouterr().err


def test_the_operator_says_yes_at_a_terminal(monkeypatch):
    answers = iter(["y", " Yes ", "n", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert [command.terminal("Is it down? ") for _ in range(4)] == [True, True, False, False]
    assert command.lags({}) == "" and command.lags({"members": "x"}) == ""
