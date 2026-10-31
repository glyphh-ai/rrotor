"""ctx.db end to end: a program that declares a SQLite database gets a file in its data folder, kept across runs."""

from __future__ import annotations

import pytest

from tests.daemon.runner.test_engine import Env
from tests.fakes import install, manifest

COUNTS = """
from rrotor import Result

def start(ctx, inputs):
    with ctx.db:
        ctx.db.execute("create table if not exists seen (n integer)")
        ctx.db.execute("insert into seen values (1)")
    return Result.done(ctx.db.execute("select count(*) from seen").fetchone()[0])
"""


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    yield e
    await e.runner.shutdown()


async def test_a_sqlite_database_is_kept_in_the_data_folder_across_runs(env):
    install(env.programs, manifest("sample", databases=["sqlite"]), COUNTS)
    first = await env.runner.call(program="sample", function="start", inputs={})
    second = await env.runner.call(program="sample", function="start", inputs={})
    assert (first.status, first.result["data"], second.result["data"]) == ("done", 1, 2)
    assert (env.programs.data("sample") / "db.sqlite3").is_file()
    assert env.launcher.specs[-1].mounts.data == env.programs.data("sample")


async def test_ctx_db_without_the_declaration_fails_the_run(env):
    install(env.programs, manifest("sample"), COUNTS)
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert (run.status, run.result["data"]["code"]) == ("failed", "E_DATABASE_UNDECLARED")
    assert env.launcher.specs[-1].mounts.data is None
