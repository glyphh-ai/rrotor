"""A program's `code` step (the server's docs/specs/programs.md, 3; rrotor 0.10.0): one function of a program's
`program.py`, run here with the step's input and answered with what it returns. It runs inside this program's
sandbox like everything of the run (the folders the call was given, the hosts this program may reach), in a process
of its own that is given none of this program's secrets: its environment is the few names a script gets
(agent_tools.PASSED), its working folder the run's own. What it returns must be JSON; what it prints is kept short
beside it. A function that raises, takes too long or returns something that is not JSON fails the step, saying why.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PASSED = ("PATH", "LANG", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy", "TMPDIR")
SECONDS = 300
OUT = 16 * 1024  # the characters of what the function printed that are kept
MARK = "\x1eglyphh-code-result\x1e"
# The child: reads {source, function, given} on stdin, runs the file as a module named program (never as __main__,
# so a file's own `if __name__ == "__main__"` does not run), calls the function, prints its answer after MARK.
RUNNER = """
import json, sys, traceback
sent = json.load(sys.stdin)
space = {"__name__": "program", "__file__": "program.py"}
try:
    exec(compile(sent["source"], "program.py", "exec"), space)
    said = {"result": space[sent["function"]](sent["given"])}
    text = json.dumps(said)
except BaseException as err:
    last = traceback.extract_tb(err.__traceback__)[-1]
    text = json.dumps({"error": f"{sent['function']} raised {type(err).__name__}: {err} (line {last.lineno})"})
sys.stdout.write(sys.argv[1] + text)
"""


def run(source: Any, function: Any, given: Any, cwd: Path) -> tuple[bool, dict[str, Any]]:
    """(True, {result, printed}) or (False, {reason, printed})."""
    if not isinstance(source, str) or not isinstance(function, str) or not function.isidentifier():
        return False, {"reason": "A code step names a function of program.py."}
    env = {k: v for k in PASSED if (v := os.environ.get(k))} | {"HOME": str(cwd)}
    sent = json.dumps({"source": source, "function": function, "given": given})
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", RUNNER, MARK],
            input=sent,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, {"reason": f"{function} did not finish within {SECONDS} seconds."}
    printed, mark, answer = done.stdout.rpartition(MARK)
    printed = ((printed if mark else done.stdout) + done.stderr)[:OUT]
    try:
        said = json.loads(answer) if mark else {}
    except ValueError:
        said = {}
    if "result" in said:
        return True, {"result": said["result"], "printed": printed}
    reason = said.get("error") or f"{function} stopped before it answered (exit {done.returncode})."
    return False, {"reason": str(reason), "printed": printed}
