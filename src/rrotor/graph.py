"""`ctx.graph`: the rrotor's knowledge graph, its semantic layer (docs/architecture.md, 5.9).

A pipeline (`"graph": "write"` in glyphh.json) turns data into nodes and edges; an app's functions and any other
program (`"graph": "read"`) search it and walk it. rrotor keeps the graph: the calls here ask it over the run's own
channel, and nothing of the graph is in the sandbox.

    ctx.graph.rows(deals, type="deal", key="id", title="name", text=["notes"],
                   links={"account_id": ("belongs_to", "account")})          # a table of records, as nodes and edges
    ctx.graph.upsert(nodes=[{"type": "doc", "key": "handbook", "title": "Handbook", "text": body}])
    ctx.graph.search("renewal terms", types=["doc"], limit=5)                # the passages to answer from
    ctx.graph.node("account", "acme"); ctx.graph.neighbours("account", "acme", depth=2)
    ctx.graph.nodes("deal", where={"stage": "won"}); ctx.graph.schema()
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from rrotor.errors import GraphError

Ask = Callable[[dict[str, Any]], dict[str, Any]]  # one call of the graph, done by rrotor: its Result as JSON
Column = str | Callable[[Mapping[str, Any]], Any]
BATCH = 500  # the nodes or edges of one write
BATCH_BYTES = 4 * 1024 * 1024  # and its size, as JSON: a write is one message on the run's channel
WRITES = frozenset({"upsert", "delete"})


def _nowhere(_: dict[str, Any]) -> dict[str, Any]:
    return {"status": "failed", "data": {"code": "E_GRAPH", "reason": "No graph is kept here."}}


def plain(value: Any) -> Any:
    """A value as JSON carries it: what JSON has no word for (a date, a Decimal) as its text."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(v) for v in value]
    return str(value)


def _read(row: Mapping[str, Any], column: Column | None) -> Any:
    if column is None:
        return None
    return column(row) if callable(column) else row.get(column)


def shape(
    rows: Iterable[Mapping[str, Any]],
    *,
    type: str,
    key: Column,
    title: Column | None = None,
    text: Iterable[str] = (),
    props: Iterable[str] | None = None,
    links: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Records as nodes and edges: each row a node of `type`, named by its `key` column, its `title` column its
    title, its `text` columns its text, the rest (or the `props` columns) its props. `links` turns a column that names
    another record into an edge: `{"account_id": ("belongs_to", "account")}` links the row to the account that id
    names (`("has_deal", "account", "in")` points the edge the other way); a list links each; an empty one, none.
    A row without a key is left out."""
    links = dict(links or {})
    words, props = list(text), list(props) if props is not None else None
    taken = {*words, *links, *(c for c in (key, title) if isinstance(c, str))}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for row in rows:
        named = _read(row, key)
        if named is None or named == "":
            continue
        here = {"type": type, "key": str(named)}
        kept = props if props is not None else [c for c in row if c not in taken]
        node: dict[str, Any] = {**here, "props": {str(c): plain(row.get(c)) for c in kept if row.get(c) is not None}}
        heading = _read(row, title)
        if heading is not None:
            node["title"] = str(heading)
        if words:
            node["text"] = "\n\n".join(str(row[c]) for c in words if row.get(c) not in (None, ""))
        nodes.append(node)
        for column, (rel, other, *way) in links.items():
            value = row.get(column)
            for one in value if isinstance(value, list | tuple | set | frozenset) else [value]:
                if one is None or one == "":
                    continue
                there = {"type": other, "key": str(one)}
                inward = way[:1] == ["in"]
                edges.append({"from": there if inward else here, "rel": rel, "to": here if inward else there})
    return nodes, edges


def batches(items: Iterable[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    """`items` a write at a time: at most BATCH of them, and at most BATCH_BYTES as JSON."""
    held: list[dict[str, Any]] = []
    size = 0
    for item in items:
        weight = len(json.dumps(item))
        if held and (len(held) == BATCH or size + weight > BATCH_BYTES):
            yield held
            held, size = [], 0
        held.append(item)
        size += weight
    if held:
        yield held


class Graph:
    """`ctx.graph`. Reading needs `"graph": "read"` (or `"write"`) in glyphh.json, writing `"graph": "write"`."""

    def __init__(self, access: str | None = None, ask: Ask | None = None) -> None:
        self._access, self._ask = access, ask or _nowhere

    def _call(self, op: str, **args: Any) -> Any:
        if self._access is None:
            raise GraphError.undeclared()
        if op in WRITES and self._access != "write":
            raise GraphError.readonly()
        answer = self._ask({"graph": op, **{k: v for k, v in args.items() if v is not None}})
        data = answer.get("data")
        if answer.get("status") != "done":
            failed: dict[str, Any] = data if isinstance(data, dict) else {}
            raise GraphError(
                str(failed.get("reason") or "The graph did not answer."), str(failed.get("code") or "E_GRAPH")
            )
        return data

    def upsert(self, nodes: Iterable[dict[str, Any]] = (), edges: Iterable[dict[str, Any]] = ()) -> dict[str, int]:
        """Nodes (`{type, key, title?, props?, text?}`) and edges (`{from: {type, key}, rel, to: {type, key},
        props?}`) written, each made or changed: a node written again keeps what this write does not name, and an edge
        to a node not written yet makes it, empty. Sent a batch at a time (500, or 4 MB), each kept whole or not at
        all. Answers how many of each were written."""
        done = {"nodes": 0, "edges": 0}
        for what, items in (("nodes", nodes), ("edges", edges)):
            for batch in batches(plain(list(items))):
                done[what] += int(self._call("upsert", **{what: batch})[what])
        return done

    def rows(self, rows: Iterable[Mapping[str, Any]], **how: Any) -> dict[str, int]:
        """Records written as nodes and edges (see `shape`: type, key, title, text, props, links)."""
        nodes, edges = shape(rows, **how)
        return self.upsert(nodes, edges)

    def delete(self, nodes: Iterable[dict[str, Any]] = (), edges: Iterable[dict[str, Any]] = ()) -> dict[str, int]:
        """Nodes (`{type, key}`, with their edges) and edges (`{from, rel, to}`) removed: how many were there."""
        done = {"nodes": 0, "edges": 0}
        for what, items in (("edges", edges), ("nodes", nodes)):
            for batch in batches(plain(list(items))):
                done[what] += int(self._call("delete", **{what: batch})[what])
        return done

    def search(
        self, query: str, types: Iterable[str] | None = None, limit: int = 10, expand: bool = False
    ) -> list[dict[str, Any]]:
        """The nodes whose words answer `query`, the best first: each `{type, key, title, props, passage, snippet,
        score}`, the passage being the text to answer from; with `expand`, the nodes it is linked to (`out`, `in`)."""
        found = self._call(
            "search", query=query, types=list(types) if types is not None else None, limit=limit, expand=expand
        )
        return list(found["hits"])

    def node(self, type: str, key: str | int) -> dict[str, Any] | None:
        """One node, whole (title, props, text, source, updated_at, and its edges `out` and `in`); None when there is
        no such node."""
        try:
            found: dict[str, Any] = self._call("node", type=type, key=str(key))
        except GraphError as err:
            if err.code == "E_GRAPH_NODE":
                return None
            raise
        return found

    def nodes(
        self, type: str, where: Mapping[str, Any] | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """The nodes of a type, the newest first: those whose props equal every value in `where`."""
        found = self._call("nodes", type=type, where=dict(where) if where else None, limit=limit, offset=offset)
        return list(found["nodes"])

    def neighbours(
        self,
        type: str,
        key: str | int,
        rel: str | None = None,
        direction: str = "both",
        depth: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        """The graph around a node: `{nodes: [… distance], edges: [{from, rel, to}]}` within `depth` edges (1 to 3),
        by `rel` alone when given, `out`, `in` or both ways."""
        found: dict[str, Any] = self._call(
            "neighbours", type=type, key=str(key), rel=rel, direction=direction, depth=depth, limit=limit
        )
        return found

    def schema(self) -> dict[str, Any]:
        """What the graph holds: its types (count, props, sources), its relations (from, rel, to, count), totals."""
        found: dict[str, Any] = self._call("schema")
        return found
