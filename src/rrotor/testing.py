"""Run a program in its own tests, with no sandbox: the same calls, Results and waits as in rrotor.

from rrotor.testing import Harness, FakeHttp
h = Harness(program, tmp_path, approvals={"start": {"write"}}, http=FakeHttp({"https://api.x/": {"ok": 1}}))
# a program with data: data=<its data folder>, home=<its folder>, clearance=<who asks, 0 to 1>
# secrets: secrets={"API_KEY": "test"} (declared: the names glyphh.json declares, those by default)
# a SQLite database (ctx.db): database=tmp_path / "db.sqlite3"
# CLIs (ctx.cli): clis={"claude": FakeCli("sent")} (stdout, a CliResult, or a function of (args, stdin));
#   FakeCli(..., allow=[...], approvals=[...]) as the CLI's manifest has them; h.cli_calls lists what ran
# the graph (ctx.graph): graph=FakeGraph("write") (or "read"), kept in memory; h.graph.nodes and h.graph.edges
#   hold what the program wrote
# the machine's folders the run is given (ctx.folders): folders=[{"path": str(tmp_path / "docs"), "access": "read"}]
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
from rrotor.graph import Graph
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


Named = tuple[str, str]  # a node: its type and key


class FakeGraph:
    """The rrotor's graph as a program's ctx.graph meets it in tests, kept in memory: `nodes` ({(type, key): node})
    and `edges` ({(from, rel, to): props}) hold what was written. Search here is plain: the nodes holding any of the
    query's words, those holding more of them first (rrotor's own ranks passages with BM25). `access` is the
    manifest's `graph`: "write", "read" or None."""

    def __init__(self, access: str | None = "write") -> None:
        self.access = access
        self.nodes: dict[Named, dict[str, Any]] = {}
        self.edges: dict[tuple[Named, str, Named], dict[str, Any]] = {}

    @staticmethod
    def _named(ref: Mapping[str, Any]) -> Named:
        return str(ref["type"]), str(ref["key"])

    def _short(self, named: Named) -> dict[str, Any]:
        node = self.nodes[named]
        return {"type": named[0], "key": named[1], "title": node["title"], "props": node["props"]}

    def _held(self, named: Named) -> dict[str, Any]:
        return self.nodes.setdefault(named, {"title": "", "props": {}, "text": ""})

    def _links(self, named: Named) -> dict[str, Any]:
        out = [{"rel": r, "edge": p, **self._short(b)} for (a, r, b), p in self.edges.items() if a == named]
        back = [{"rel": r, "edge": p, **self._short(a)} for (a, r, b), p in self.edges.items() if b == named]
        return {"out": out, "out_total": len(out), "in": back, "in_total": len(back)}

    def ask(self, ask: dict[str, Any]) -> dict[str, Any]:
        """One call of the graph, answered as rrotor answers the worker."""
        found = getattr(self, f"_{ask['graph']}")(ask)
        if found is None:
            return {"status": "failed", "data": {"code": "E_GRAPH_NODE", "reason": "There is no such node."}}
        return {"status": "done", "data": found}

    def _upsert(self, ask: dict[str, Any]) -> dict[str, Any]:
        nodes, edges = ask.get("nodes") or [], ask.get("edges") or []
        for node in nodes:
            self._held(self._named(node)).update({k: v for k, v in node.items() if k in ("title", "props", "text")})
        for edge in edges:
            ends = self._named(edge["from"]), self._named(edge["to"])
            for end in ends:
                self._held(end)
            self.edges[(ends[0], edge["rel"], ends[1])] = dict(edge.get("props") or {})
        return {"nodes": len(nodes), "edges": len(edges)}

    def _delete(self, ask: dict[str, Any]) -> dict[str, Any]:
        gone = {"nodes": 0, "edges": 0}
        for edge in ask.get("edges") or []:
            named = (self._named(edge["from"]), edge["rel"], self._named(edge["to"]))
            gone["edges"] += self.edges.pop(named, None) is not None
        for node in ask.get("nodes") or []:
            named_node = self._named(node)
            gone["nodes"] += self.nodes.pop(named_node, None) is not None
            self.edges = {e: p for e, p in self.edges.items() if named_node not in (e[0], e[2])}
        return gone

    def _search(self, ask: dict[str, Any]) -> dict[str, Any]:
        terms = [w for w in str(ask["query"]).lower().split() if w]
        hits = []
        for named, node in self.nodes.items():
            if ask.get("types") and named[0] not in ask["types"]:
                continue
            passage = "\n\n".join(p for p in (node["title"], json.dumps(node["props"]), node["text"]) if p)
            score = sum(1 for t in terms if t in passage.lower())
            if score:
                hit = {**self._short(named), "passage": passage, "snippet": passage[:200], "chunk": 0, "score": score}
                hits.append({**hit, **(self._links(named) if ask.get("expand") else {})})
        hits.sort(key=lambda h: -h["score"])
        return {"hits": hits[: int(ask.get("limit") or 10)]}

    def _node(self, ask: dict[str, Any]) -> dict[str, Any] | None:
        named = self._named(ask)
        if named not in self.nodes:
            return None
        node = self.nodes[named]
        return {**self._short(named), "text": node["text"], "source": "", "updated_at": "", **self._links(named)}

    def _nodes(self, ask: dict[str, Any]) -> dict[str, Any]:
        where = ask.get("where") or {}
        found = [
            self._short(n)
            for n, node in self.nodes.items()
            if n[0] == ask["type"] and all(node["props"].get(k) == v for k, v in where.items())
        ]
        start = int(ask.get("offset") or 0)
        return {"nodes": found[start : start + int(ask.get("limit") or 100)], "total": len(found)}

    def _neighbours(self, ask: dict[str, Any]) -> dict[str, Any] | None:
        start, rel, way = self._named(ask), ask.get("rel"), ask.get("direction") or "both"
        if start not in self.nodes:
            return None
        distance = {start: 0}
        edges = []
        for step in range(1, int(ask.get("depth") or 1) + 1):
            for a, r, b in list(self.edges):
                if rel not in (None, r):
                    continue
                for here, there in ((a, b), (b, a)):
                    if distance.get(here) == step - 1 and way in ("both", "out" if here == a else "in"):
                        distance.setdefault(there, step)
                        edges.append((a, r, b))
        named = [
            {
                "from": dict(zip(("type", "key"), a, strict=True)),
                "rel": r,
                "to": dict(zip(("type", "key"), b, strict=True)),
            }
            for a, r, b in dict.fromkeys(edges)
        ]
        return {"nodes": [{**self._short(n), "distance": d} for n, d in distance.items()], "edges": named}

    def _schema(self, _: dict[str, Any]) -> dict[str, Any]:
        types = sorted({n[0] for n in self.nodes})
        listed = [
            {
                "type": t,
                "count": sum(1 for n in self.nodes if n[0] == t),
                "props": sorted({k for n, node in self.nodes.items() if n[0] == t for k in node["props"]}),
                "sources": [],
            }
            for t in types
        ]
        counted: dict[tuple[str, str, str], int] = {}
        for a, r, b in self.edges:
            counted[(a[0], r, b[0])] = counted.get((a[0], r, b[0]), 0) + 1
        relations = [{"from": a, "rel": r, "to": b, "count": n} for (a, r, b), n in sorted(counted.items())]
        return {
            "types": listed,
            "relations": relations,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "chunks": 0,
        }


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
        graph: FakeGraph | None = None,
        folders: Iterable[Mapping[str, str]] = (),
    ) -> None:
        self.program, self.files, self.database = program, files, database
        self.folders = tuple(folders)  # ctx.folders: the machine's folders the run is given
        self.graph = graph or FakeGraph(None)  # ctx.graph: a manifest that names none refuses, as on rrotor
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
            graph=Graph(self.graph.access, self.graph.ask),
            folders=self.folders,
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
