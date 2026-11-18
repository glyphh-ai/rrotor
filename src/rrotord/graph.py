"""The graph: this rrotor's semantic layer (docs/architecture.md, 5.9).

One knowledge graph for the whole rrotor, in `/data/graph/graph.sqlite3`: **nodes** (a type and a key, with a title,
properties and text), **edges** between them (a relation, with properties) and each node's words cut into **chunks**
that SQLite's FTS5 indexes. A pipeline (a program whose manifest says `"graph": "write"`) writes it with `ctx.graph`;
an app's functions (`"graph": "read"`) and Claude (`graph_search`, `graph_node`, `graph_neighbours`, `graph_nodes`,
`graph_schema`) read it. Retrieval is ranked full-text search (BM25) over the chunks, each hit answered with its
passage and, when asked, the nodes it is linked to: the passages a model is given to answer from. No embeddings: no
model runs here.

A write is one transaction: a batch is kept whole or not at all. A node is its `(type, key)`: writing it again
replaces what the write names (title, props, text) and keeps the rest, so a pipeline can run again and again. An edge
to a node not written yet makes that node, empty, to be filled when its own pipeline runs.

The graph is node-local, as the vault is: it is not in a cluster's replicated log, so a cluster keeps it on its head
(every call of a program that names the graph runs there, and a follower sends the API's graph calls to it)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rrotord.errors import BadRequest, NotFound
from rrotord.manifest import NAME

KIND = re.compile(NAME)  # a node's type and an edge's relation
PROP = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .-]{0,62}$")  # a property a `nodes` filter may name
WORD = re.compile(r"\w+")
KEY_MAX, TITLE_MAX = 256, 300
PROPS_MAX = 64 * 1024  # bytes of one node's or edge's properties, as JSON
TEXT_MAX = 1024 * 1024  # characters of one node's text
NODES_MAX, EDGES_MAX = 2000, 10000  # in one write
CHUNK = 1200  # characters of one chunk: a passage a search answers
LIMIT_MAX, DEPTH_MAX = 200, 3
LINKS = 20  # the linked nodes a search's hit or a node's side shows
SAMPLE = 200  # the nodes of a type its property names are read from
WAYS = (("src", "dst"), ("dst", "src"))  # an edge followed out of a node, and into it
WRITES = frozenset({"upsert", "delete"})
OPS = WRITES | {"search", "node", "nodes", "neighbours", "schema"}

SCHEMA = """
create table if not exists nodes (
  id integer primary key, type text not null, key text not null, title text not null default '',
  props text not null default '{}', text text not null default '', source text not null default '',
  updated_at text not null, unique (type, key));
create table if not exists edges (
  src integer not null references nodes(id) on delete cascade, rel text not null,
  dst integer not null references nodes(id) on delete cascade, props text not null default '{}',
  source text not null default '', updated_at text not null, primary key (src, rel, dst)) without rowid;
create index if not exists edges_dst on edges (dst, rel);
create table if not exists chunks (
  id integer primary key, node integer not null references nodes(id) on delete cascade, seq integer not null,
  text text not null);
create index if not exists chunks_node on chunks (node);
create virtual table if not exists chunks_fts using fts5 (
  text, content='chunks', content_rowid='id', tokenize='porter unicode61');
create trigger if not exists chunks_ai after insert on chunks begin
  insert into chunks_fts (rowid, text) values (new.id, new.text); end;
create trigger if not exists chunks_ad after delete on chunks begin
  insert into chunks_fts (chunks_fts, rowid, text) values ('delete', old.id, old.text); end;
"""


def refused(message: str) -> BadRequest:
    return BadRequest(message, code="E_GRAPH")


def kind(value: Any, what: str) -> str:
    if not isinstance(value, str) or not KIND.match(value):
        raise refused(f"{what} is a name: lower-case letters, digits and _, starting with a letter ({value!r}).")
    return value


def ref(value: Any, what: str = "A node") -> tuple[str, str]:
    """A node named by its type and key: `{type, key}`."""
    if not isinstance(value, dict):
        raise refused(f"{what} is named {{type, key}}.")
    key = value.get("key")
    if isinstance(key, int) and not isinstance(key, bool):
        key = str(key)
    if not isinstance(key, str) or not key or len(key) > KEY_MAX or any(ord(c) < 32 for c in key):
        raise refused(f"{what}'s key is 1 to {KEY_MAX} characters, with no control characters.")
    return kind(value.get("type"), f"{what}'s type"), key


def props_of(value: Any, what: str) -> str:
    if not isinstance(value, dict):
        raise refused(f"{what}'s props is an object.")
    text = json.dumps(value, separators=(",", ":"), sort_keys=True)
    if len(text.encode()) > PROPS_MAX:
        raise refused(f"{what}'s props are over 64 KB.")
    return text


def text_of(value: Any, what: str, most: int) -> str:
    if not isinstance(value, str) or len(value) > most:
        raise refused(f"{what} is text of at most {most} characters.")
    return value


def listed(value: Any, what: str, most: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > most:
        raise refused(f"{what} is a list of at most {most}.")
    return value


def bounded(value: Any, what: str, default: int, most: int, least: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not least <= value <= most:
        raise refused(f"{what} is a whole number from {least} to {most}.")
    return value


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """`text` cut into passages of at most `size` characters: whole paragraphs together while they fit, a longer one
    cut at its last space."""
    found: list[str] = []
    held = ""
    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text)):
        while len(paragraph) > size:
            if held:
                found.append(held)
                held = ""
            cut = paragraph.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            found.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if not paragraph:
            continue
        if held and len(held) + 2 + len(paragraph) > size:
            found.append(held)
            held = ""
        held = f"{held}\n\n{paragraph}" if held else paragraph
    if held:
        found.append(held)
    return found


def words(title: str, props: dict[str, Any], text: str) -> str:
    """What a node is found by: its title, its properties' plain values (`name: value`) and its text."""
    plain = [f"{k}: {v}" for k, v in props.items() if isinstance(v, str | int | float) and not isinstance(v, bool)]
    return "\n\n".join(part for part in (title, "\n".join(plain), text) if part)


def match(query: str) -> str | None:
    """A search's words as an FTS5 query: any of them, each quoted (so none is read as FTS5 syntax)."""
    found = list(dict.fromkeys(w.lower() for w in WORD.findall(query)))[:32]
    return " OR ".join(f'"{w}"' for w in found) if found else None


def marks(many: Iterable[Any]) -> str:
    return ",".join("?" for _ in many)


class Graph:
    def __init__(self, data: Path, now: Callable[[], dt.datetime] | None = None) -> None:
        self.path = data / "graph" / "graph.sqlite3"
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()  # one statement at a time: calls come from worker threads

    def _open(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("pragma journal_mode=wal")
            db.execute("pragma foreign_keys=on")
            db.executescript(SCHEMA)
            self._db = db
        return self._db

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._open()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One transaction: what it writes is kept whole, or not at all."""
        with self._lock:
            db = self._open()
            db.execute("begin immediate")
            try:
                yield db
            except BaseException:
                db.execute("rollback")
                raise
            db.execute("commit")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @staticmethod
    def _id(db: sqlite3.Connection, node: tuple[str, str]) -> int | None:
        row = db.execute("select id from nodes where type = ? and key = ?", node).fetchone()
        return int(row["id"]) if row else None

    def _ensure(self, db: sqlite3.Connection, node: tuple[str, str], source: str, at: str) -> int:
        """The node's id: the node made, empty, when an edge names one not written yet."""
        found = self._id(db, node)
        if found is not None:
            return found
        made = db.execute(
            "insert into nodes (type, key, source, updated_at) values (?, ?, ?, ?)", (*node, source, at)
        ).lastrowid
        assert made is not None  # noqa: S101  (an insert has a row id)
        return made

    def _put(self, db: sqlite3.Connection, given: dict[str, Any], source: str, at: str) -> None:
        node = ref(given)
        unknown = sorted(set(given) - {"type", "key", "title", "props", "text"})
        if unknown:
            raise refused(f"A node has type, key, title, props and text, not {', '.join(unknown)}.")
        node_id = self._ensure(db, node, source, at)
        row = db.execute("select title, props, text from nodes where id = ?", (node_id,)).fetchone()
        title = text_of(given["title"], "A node's title", TITLE_MAX) if "title" in given else row["title"]
        props = props_of(given["props"], "A node") if "props" in given else row["props"]
        text = text_of(given["text"], "A node's text", TEXT_MAX) if "text" in given else row["text"]
        db.execute(
            "update nodes set title = ?, props = ?, text = ?, source = ?, updated_at = ? where id = ?",
            (title, props, text, source, at, node_id),
        )
        db.execute("delete from chunks where node = ?", (node_id,))
        passages = chunks(words(title, json.loads(props), text))
        db.executemany(
            "insert into chunks (node, seq, text) values (?, ?, ?)", [(node_id, n, p) for n, p in enumerate(passages)]
        )

    def _link(self, db: sqlite3.Connection, given: Any, source: str, at: str) -> None:
        if not isinstance(given, dict) or set(given) - {"from", "rel", "to", "props"}:
            raise refused("An edge is {from: {type, key}, rel, to: {type, key}, props?}.")
        rel = kind(given.get("rel"), "An edge's rel")
        src = self._ensure(db, ref(given.get("from"), "An edge's from"), source, at)
        dst = self._ensure(db, ref(given.get("to"), "An edge's to"), source, at)
        props = props_of(given.get("props", {}), "An edge")
        db.execute(
            "insert into edges (src, rel, dst, props, source, updated_at) values (?, ?, ?, ?, ?, ?) "
            "on conflict (src, rel, dst) do update set props = excluded.props, source = excluded.source, "
            "updated_at = excluded.updated_at",
            (src, rel, dst, props, source, at),
        )

    def upsert(self, nodes: Any = None, edges: Any = None, source: str = "") -> dict[str, Any]:
        """Nodes and edges written, each made or changed; all of them or none."""
        nodes = listed(nodes, "nodes", NODES_MAX)
        edges = listed(edges, "edges", EDGES_MAX)
        at = self._stamp()
        with self._write() as db:
            for node in nodes:
                self._put(db, node, source, at)
            for edge in edges:
                self._link(db, edge, source, at)
        return {"nodes": len(nodes), "edges": len(edges)}

    def delete(self, nodes: Any = None, edges: Any = None) -> dict[str, Any]:
        """Nodes removed (with their edges and chunks) and edges removed; one not there is not counted."""
        gone = {"nodes": 0, "edges": 0}
        with self._write() as db:
            for given in listed(edges, "edges", EDGES_MAX):
                if not isinstance(given, dict):
                    raise refused("An edge is {from: {type, key}, rel, to: {type, key}}.")
                src = self._id(db, ref(given.get("from"), "An edge's from"))
                dst = self._id(db, ref(given.get("to"), "An edge's to"))
                rel = kind(given.get("rel"), "An edge's rel")
                done = db.execute("delete from edges where src = ? and rel = ? and dst = ?", (src, rel, dst))
                gone["edges"] += done.rowcount
            for given in listed(nodes, "nodes", NODES_MAX):
                gone["nodes"] += db.execute("delete from nodes where type = ? and key = ?", ref(given)).rowcount
        return gone

    @staticmethod
    def _short(row: sqlite3.Row) -> dict[str, Any]:
        return {"type": row["type"], "key": row["key"], "title": row["title"], "props": json.loads(row["props"])}

    def _links(self, db: sqlite3.Connection, node_id: int, limit: int = LINKS) -> dict[str, Any]:
        """A node's edges, each way: the nodes they reach, the newest first, and how many there are."""
        sides: dict[str, Any] = {}
        for side, here, there in (("out", "src", "dst"), ("in", "dst", "src")):
            rows = db.execute(
                f"select e.rel, e.props as edge_props, n.type, n.key, n.title, n.props from edges e "  # noqa: S608
                f"join nodes n on n.id = e.{there} where e.{here} = ? order by e.updated_at desc, n.id limit ?",
                (node_id, limit),
            ).fetchall()
            total = db.execute(f"select count(*) from edges where {here} = ?", (node_id,)).fetchone()[0]  # noqa: S608
            sides[side] = [{"rel": r["rel"], "edge": json.loads(r["edge_props"]), **self._short(r)} for r in rows]
            sides[f"{side}_total"] = total
        return sides

    def node(self, type: Any, key: Any) -> dict[str, Any]:
        """One node, whole: its title, props, text, who wrote it and when, and its edges each way."""
        named = ref({"type": type, "key": key})
        with self._read() as db:
            row = db.execute("select * from nodes where type = ? and key = ?", named).fetchone()
            if row is None:
                raise NotFound(f"There is no {named[0]} {named[1]!r} in the graph.", code="E_GRAPH_NODE")
            return {
                **self._short(row),
                "text": row["text"],
                "source": row["source"],
                "updated_at": row["updated_at"],
                **self._links(db, row["id"], LIMIT_MAX),
            }

    def nodes(self, type: Any, where: Any = None, limit: Any = None, offset: Any = None) -> dict[str, Any]:
        """The nodes of a type, the newest first, those whose props equal every value in `where`; and how many."""
        kind(type, "type")
        where = {} if where is None else where
        if not isinstance(where, dict) or not all(isinstance(k, str) and PROP.match(k) for k in where):
            raise refused("where is {a property's name: the value it has}.")
        if any(isinstance(v, dict | list) for v in where.values()):
            raise refused("where compares a property with a text, a number, true, false or null.")
        clause, args = "type = ?", [type]
        for name, value in where.items():
            clause += " and json_extract(props, ?) is ?"
            args += [f'$."{name}"', value]
        limit, offset = bounded(limit, "limit", 100, LIMIT_MAX), bounded(offset, "offset", 0, 10**9, 0)
        with self._read() as db:
            rows = db.execute(
                f"select * from nodes where {clause} order by updated_at desc, id desc limit ? offset ?",  # noqa: S608
                (*args, limit, offset),
            ).fetchall()
            total = db.execute(f"select count(*) from nodes where {clause}", args).fetchone()[0]  # noqa: S608
        return {"nodes": [self._short(r) for r in rows], "total": total}

    def search(self, query: Any, types: Any = None, limit: Any = None, expand: Any = False) -> dict[str, Any]:
        """The nodes whose words answer `query`, the best first: each with the passage that matched, a snippet of it
        and its score; with `expand`, the nodes each is linked to."""
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise refused("query is what to look for: text of 1 to 2000 characters.")
        types = [kind(t, "A type") for t in listed(types, "types", 50)]
        limit = bounded(limit, "limit", 10, 50)
        asked = match(query)
        if asked is None:
            return {"hits": []}
        narrowed = f"and n.type in ({marks(types)})" if types else ""
        with self._read() as db:
            rows = db.execute(
                "select n.id, n.type, n.key, n.title, n.props, c.seq, c.text as passage, "  # noqa: S608
                "snippet(chunks_fts, 0, '[', ']', '…', 24) as snippet, bm25(chunks_fts) as rank "
                "from chunks_fts join chunks c on c.id = chunks_fts.rowid join nodes n on n.id = c.node "
                f"where chunks_fts match ? {narrowed} order by rank limit ?",
                (asked, *types, limit * 8),
            ).fetchall()
            hits: dict[int, dict[str, Any]] = {}
            for row in rows:
                if row["id"] in hits or len(hits) == limit:
                    continue
                hit = {**self._short(row), "passage": row["passage"], "snippet": row["snippet"], "chunk": row["seq"]}
                hit["score"] = round(-float(row["rank"]), 4)
                if expand:
                    hit.update(self._links(db, row["id"]))
                hits[row["id"]] = hit
        return {"hits": list(hits.values())}

    def neighbours(
        self,
        type: Any,
        key: Any,
        rel: Any = None,
        direction: Any = None,
        depth: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        """The graph around a node: the nodes within `depth` edges of it (by `rel` alone, when given; `out`, `in` or
        both ways), each with its distance, and the edges among them."""
        named = ref({"type": type, "key": key})
        rel = None if rel is None else kind(rel, "rel")
        direction = "both" if direction is None else direction
        if direction not in ("out", "in", "both"):
            raise refused("direction is out, in or both.")
        depth, limit = bounded(depth, "depth", 1, DEPTH_MAX), bounded(limit, "limit", 100, LIMIT_MAX)
        ways = [("src", "dst")] if direction == "out" else [("dst", "src")] if direction == "in" else list(WAYS)
        with self._read() as db:
            start = self._id(db, named)
            if start is None:
                raise NotFound(f"There is no {named[0]} {named[1]!r} in the graph.", code="E_GRAPH_NODE")
            distance = {start: 0}
            edges: dict[tuple[int, str, int], None] = {}
            frontier = [start]
            for step in range(1, depth + 1):
                reached: list[int] = []
                for here, there in ways:
                    rows = db.execute(
                        f"select src, rel, dst from edges where {here} in ({marks(frontier)}) "  # noqa: S608
                        f"{'and rel = ?' if rel else ''} order by updated_at desc",
                        (*frontier, *([rel] if rel else [])),
                    ).fetchall()
                    for row in rows:
                        other = row[there]
                        if other not in distance:
                            if len(distance) > limit:
                                continue
                            distance[other] = step
                            reached.append(other)
                        edges[(row["src"], row["rel"], row["dst"])] = None
                frontier = reached
                if not frontier:
                    break
            found = {
                r["id"]: r
                for r in db.execute(f"select * from nodes where id in ({marks(distance)})", list(distance))  # noqa: S608
            }
        name = {i: {"type": r["type"], "key": r["key"]} for i, r in found.items()}
        return {
            "nodes": [{**self._short(found[i]), "distance": d} for i, d in distance.items()],
            "edges": [{"from": name[s], "rel": r, "to": name[d]} for s, r, d in edges if s in name and d in name],
        }

    def schema(self) -> dict[str, Any]:
        """What the graph holds: each type of node with how many there are, the properties its nodes carry and the
        programs that wrote them; each relation between two types with how many; and the totals."""
        with self._read() as db:
            types = []
            for row in db.execute("select type, count(*) as n from nodes group by type order by type").fetchall():
                sample = db.execute(
                    "select props, source from nodes where type = ? order by updated_at desc limit ?",
                    (row["type"], SAMPLE),
                ).fetchall()
                names = sorted({k for s in sample for k in json.loads(s["props"])})
                sources = sorted({s["source"] for s in sample if s["source"]})
                types.append({"type": row["type"], "count": row["n"], "props": names, "sources": sources})
            relations = [
                {"from": r["a"], "rel": r["rel"], "to": r["b"], "count": r["n"]}
                for r in db.execute(
                    "select a.type as a, e.rel, b.type as b, count(*) as n from edges e join nodes a on a.id = e.src "
                    "join nodes b on b.id = e.dst group by a.type, e.rel, b.type order by a.type, e.rel, b.type"
                ).fetchall()
            ]
            counts = {
                name: db.execute(f"select count(*) from {name}").fetchone()[0]  # noqa: S608
                for name in ("nodes", "edges", "chunks")
            }
        return {"types": types, "relations": relations, **counts}

    def do(self, op: str, a: dict[str, Any], source: str = "") -> dict[str, Any]:
        """One operation by its name, its arguments as the API, an MCP tool or a program's ctx.graph sent them; a
        write is marked with `source`, the program that made it (none: Claude, or a caller of the API)."""
        match op:
            case "upsert":
                return self.upsert(a.get("nodes"), a.get("edges"), source)
            case "delete":
                return self.delete(a.get("nodes"), a.get("edges"))
            case "search":
                return self.search(a.get("query"), a.get("types"), a.get("limit"), a.get("expand"))
            case "node":
                return self.node(a.get("type"), a.get("key"))
            case "nodes":
                return self.nodes(a.get("type"), a.get("where"), a.get("limit"), a.get("offset"))
            case "neighbours":
                rel, way, depth = a.get("rel"), a.get("direction"), a.get("depth")
                return self.neighbours(a.get("type"), a.get("key"), rel, way, depth, a.get("limit"))
            case "schema":
                return self.schema()
        raise refused(f"There is no graph operation {op!r}: {', '.join(sorted(OPS))}.")

    async def call(self, op: str, args: dict[str, Any], source: str = "") -> dict[str, Any]:
        """`do`, off the event loop: a large write does not hold the daemon up."""
        return await asyncio.to_thread(self.do, op, args, source)
