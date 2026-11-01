"""Run a program in its own tests, with no sandbox: the same calls, Results and waits as in rrotor.

from rrotor.testing import Harness, FakeHttp
h = Harness(program, tmp_path, approvals={"start": {"write"}}, http=FakeHttp({"https://api.x/": {"ok": 1}}))
# a program with data: data=<its data folder>, home=<its folder>, clearance=<who asks, 0 to 1>
# secrets: secrets={"API_KEY": "test"} (declared: the names glyphh.json declares, those by default)
# a SQLite database (ctx.db): database=tmp_path / "db.sqlite3"
# CLIs (ctx.cli): clis={"claude": FakeCli("sent")} (stdout, a CliResult, or a function of (args, stdin));
#   FakeCli(..., allow=[...], approvals=[...]) as the CLI's manifest has them; h.cli_calls lists what ran
r = h.call("start", {"day": "2026-10-14"})   # Result needs_approval
r = h.resume({"approved": True})               # Result done
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from rrotor.ctx import CliResult, Ctx, RunState, Secrets
from rrotor.http import Http, Response
from rrotor.invoke import Outcome, invoke, lifecycle
from rrotor.result import Result

Answer = Response | Any | Callable[[str, str, bytes | None], Response]


class _Res:
    def __init__(self, r: Response) -> None:
        self.status, self._r = r.status, r
        self.headers = r.headers

    def read(self) -> bytes:
        return self._r.body

    def __enter__(self) -> _Res:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Opener:
    def __init__(self, fake: FakeHttp) -> None:
        self.fake = fake

    def open(self, req: Any, data: bytes | None = None, timeout: float = 30.0) -> _Res:
        return _Res(self.fake.answer(req.get_method(), req.full_url, req.data))


class FakeHttp(Http):
    """Answers by the longest address prefix: a Response, a function of (method, url, body), or JSON."""

    def __init__(self, routes: Mapping[str, Answer], hosts: frozenset[str] | None = None) -> None:
        from urllib.parse import urlsplit

        self.routes = dict(routes)
        self.calls: list[tuple[str, str, bytes | None]] = []
        declared = hosts if hosts is not None else frozenset(urlsplit(u).hostname or "" for u in routes)
        super().__init__(declared, None, opener=_Opener(self))

    def answer(self, method: str, url: str, body: bytes | None) -> Response:
        self.calls.append((method, url, body))
        found = sorted((p for p in self.routes if url.startswith(p)), key=len, reverse=True)
        if not found:
            return Response(404, b"no route in FakeHttp")
        value = self.routes[found[0]]
        if isinstance(value, Response):
            return value
        if callable(value):
            return value(method, url, body)
        return Response(200, json.dumps(value).encode(), {"Content-Type": "application/json"})


CliAnswer = CliResult | str | Callable[[list[str], str | None], "CliResult | str"]


class FakeCli:
    """A CLI as a program's ctx.cli meets it in tests: its answer (stdout of a command that exits 0, a CliResult, or a
    function of the arguments and stdin), and the allow and approval patterns its manifest would have."""

    def __init__(self, answer: CliAnswer = "", *, allow: Iterable[str] = (), approvals: Iterable[str] = ()) -> None:
        self.answer, self.allow, self.approvals = answer, list(allow), list(approvals)

    def run(self, args: list[str], stdin: str | None) -> CliResult:
        got = self.answer(args, stdin) if callable(self.answer) else self.answer
        return got if isinstance(got, CliResult) else CliResult(0, str(got), "")


class Harness:
    """One run of a program module: `call`, then `resume` with each answer, as rrotor would."""

    def __init__(
        self,
        program: ModuleType,
        files: Path,
        *,
        approvals: Mapping[str, set[str]] | None = None,
        http: Http | None = None,
        today: Callable[[], Any] | None = None,
        data: Path | None = None,
        home: Path | None = None,
        clearance: float = 1.0,
        caller: str = "claude",
        secrets: Mapping[str, str] | None = None,
        declared: Iterable[str] | None = None,
        database: Path | None = None,
        clis: Mapping[str, FakeCli | None] | None = None,
    ) -> None:
        self.program, self.files, self.database = program, files, database
        self.clis = dict(clis or {})  # the CLIs the manifest names: None for one not on the rrotor
        self.cli_calls: list[tuple[str, list[str], str | None]] = []
        self.secrets = Secrets(declared if declared is not None else (secrets or {}), secrets)
        self.data, self.clearance, self.caller = data, clearance, caller
        self.home = home if home is not None else Path(str(getattr(program, "__file__", "."))).parent
        self.approvals = {k: frozenset(v) for k, v in (approvals or {}).items()}
        self.http = http or FakeHttp({})
        self.today = today
        self.state = RunState()
        self.logs: list[dict[str, Any]] = []
        self.function = ""
        self.inputs: dict[str, Any] = {}
        self.pending: dict[str, str] | None = None
        self.ended = False

    def _ctx(self, answer: Any = None) -> Ctx:
        def sink(op: str, data: dict[str, Any]) -> None:
            if op == "log":
                self.logs.append(data)

        return Ctx(
            inputs=self.inputs,
            approvals=self.approvals.get(self.function, frozenset()),
            state=self.state,
            files=self.files,
            http=self.http,
            answer=answer,
            sink=sink,
            today=self.today,
            data=self.data,
            home=self.home,
            clearance=self.clearance,
            caller=self.caller,
            secrets=self.secrets,
            database=self.database,
            clis={k: {"allow": c.allow, "approvals": c.approvals} if c else None for k, c in self.clis.items()},
            request=self._cli,
        )

    def _cli(self, ask: dict[str, Any]) -> dict[str, Any]:
        name, args, stdin = ask["cli"], list(ask["args"]), ask.get("stdin")
        fake = self.clis[name]
        assert fake is not None  # noqa: S101  (ctx.cli refuses a CLI that is not here first)
        self.cli_calls.append((name, args, stdin))
        return {"status": "done", "data": fake.run(args, stdin).to_json()}

    def _run(self, answer: Any) -> Result:
        ctx = self._ctx(answer)
        failed = lifecycle("load", getattr(self.program, "load", None), ctx)
        outcome = (
            Outcome(failed) if failed else invoke(self.function, getattr(self.program, self.function), ctx, self.inputs)
        )
        self.pending = outcome.pending
        if not outcome.result.waiting:
            lifecycle("end", getattr(self.program, "end", None), ctx)
            self.ended = True
        ctx.close()
        return outcome.result

    def call(self, function: str, inputs: dict[str, Any] | None = None) -> Result:
        self.function, self.inputs = function, dict(inputs or {})
        return self._run(None)

    def resume(self, answer: Any) -> Result:
        """`continue` with `answer`: an approval's is {"approved": bool}, an input's is the value."""
        if self.pending is None:
            raise RuntimeError("This run is not waiting.")
        self.state.record(self.pending, answer)
        return self._run(answer)
