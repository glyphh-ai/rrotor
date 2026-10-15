import sys

from rrotord.proc import Done, run
from tests.fakes import token


def test_why_prefers_stderr_then_stdout_then_the_code():
    assert Done(1, "out", "err").why() == "err"
    assert Done(1, "out", " ").why() == "out"
    assert Done(3, "", "").why() == "exit 3"


def test_why_is_masked_and_cut():
    secret = token()
    assert secret not in Done(1, "", f"Bearer {secret}").why()
    assert Done(1, "", "x" * 50).why(10) == "x" * 10


async def test_run_answers_code_and_output(tmp_path):
    done = await run(
        [sys.executable, "-c", "import os,sys;print(os.getcwd());sys.stderr.write('e');sys.exit(4)"],
        cwd=tmp_path,
        env={},
    )
    assert (done.code, done.stdout.strip().endswith(tmp_path.name), done.stderr) == (4, True, "e")


async def test_run_a_command_past_its_time_is_killed():
    done = await run([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.2)
    assert done.code == -9 and "took longer than 0.2 s" in done.stderr
