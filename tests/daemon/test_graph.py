"""The graph (rrotord/graph.py): nodes and edges written whole or not at all, a node rewritten keeping what a write
does not name, searched by its words with the passage that matched, read with its edges, walked to its neighbours,
listed by type and property, and described by its schema; what is misshapen refused with E_GRAPH."""

from __future__ import annotations

import datetime as dt

import pytest

from rrotord.errors import BadRequest, NotFound
from rrotord.graph import CHUNK, Graph, chunks, match, words

ACME = {"type": "account", "key": "acme"}
DEAL = {"type": "deal", "key": "d1"}


@pytest.fixture
def graph(tmp_path):
    clock = iter(dt.datetime(2026, 11, 18, 9, 0, s, tzinfo=dt.UTC) for s in range(60))
    g = Graph(tmp_path, now=lambda: next(clock))
    yield g
    g.close()


def seed(g: Graph) -> None:
    g.upsert(
        nodes=[
            {**ACME, "title": "Acme Corp", "props": {"tier": "gold", "seats": 40, "active": True, "tags": ["a"]}},
            {**DEAL, "title": "Renewal 2027", "props": {"stage": "won"}, "text": "The renewal closed in March."},
            {"type": "deal", "key": "d2", "title": "Expansion", "props": {"stage": "open"}},
            {"type": "person", "key": 7, "title": "Dana Ruiz", "text": "Dana signs every renewal."},
        ],
        edges=[
            {"from": DEAL, "rel": "belongs_to", "to": ACME, "props": {"since": "2026"}},
            {"from": {"type": "deal", "key": "d2"}, "rel": "belongs_to", "to": ACME},
            {"from": {"type": "person", "key": "7"}, "rel": "works_at", "to": ACME},
        ],
        source="crm_sync",
    )


def test_a_write_is_read_back_whole_with_its_edges(graph, tmp_path):
    assert graph.schema() == {"types": [], "relations": [], "nodes": 0, "edges": 0, "chunks": 0}
    seed(graph)
    got = graph.node("account", "acme")
    assert (got["title"], got["props"]["seats"], got["source"]) == ("Acme Corp", 40, "crm_sync")
    assert got["updated_at"] == "2026-11-18T09:00:00+00:00"
    assert (got["out"], got["out_total"], got["in_total"]) == ([], 0, 3)
    assert {(e["rel"], e["type"], e["key"]) for e in got["in"]} == {
        ("belongs_to", "deal", "d1"),
        ("belongs_to", "deal", "d2"),
        ("works_at", "person", "7"),
    }
    assert next(e for e in got["in"] if e["key"] == "d1")["edge"] == {"since": "2026"}
    assert graph.node("deal", "d1")["out"][0]["title"] == "Acme Corp"
    assert (tmp_path / "graph" / "graph.sqlite3").is_file()


def test_writing_a_node_again_keeps_what_the_write_does_not_name(graph):
    seed(graph)
    graph.upsert(nodes=[{**DEAL, "props": {"stage": "lost"}}], source="fixer")
    got = graph.node("deal", "d1")
    assert (got["title"], got["props"], got["text"]) == (
        "Renewal 2027",
        {"stage": "lost"},
        "The renewal closed in March.",
    )
    assert (got["source"], got["updated_at"]) == ("fixer", "2026-11-18T09:00:01+00:00")
    graph.upsert(edges=[{"from": DEAL, "rel": "belongs_to", "to": ACME, "props": {"since": "2027"}}])
    assert graph.node("deal", "d1")["out"][0]["edge"] == {"since": "2027"}
    assert graph.schema()["edges"] == 3


def test_an_edge_to_a_node_not_written_yet_makes_it_empty(graph):
    graph.upsert(edges=[{"from": DEAL, "rel": "belongs_to", "to": ACME}])
    assert graph.node("account", "acme")["title"] == ""
    graph.upsert(nodes=[{**ACME, "title": "Acme Corp"}])
    assert graph.node("account", "acme")["in_total"] == 1


def test_a_write_with_one_bad_part_keeps_none_of_it(graph):
    with pytest.raises(BadRequest, match="rel is a name"):
        graph.upsert(nodes=[{**ACME, "title": "Acme"}], edges=[{"from": DEAL, "rel": "Belongs To", "to": ACME}])
    assert graph.schema()["nodes"] == 0


def test_search_answers_the_passage_that_matched_the_best_first(graph):
    seed(graph)
    hits = graph.search("renewals")["hits"]  # stemmed: renewal, renewals
    assert [h["key"] for h in hits] == ["d1", "7"] or [h["key"] for h in hits] == ["7", "d1"]
    first = next(h for h in hits if h["key"] == "d1")
    assert "[renewal]" in first["snippet"].lower() and "closed in March" in first["passage"]
    assert first["score"] >= 0 and first["chunk"] == 0 and "out" not in first
    gold = graph.search("gold")["hits"]  # a property's value is found too; a rare word scores above a common one
    assert [h["key"] for h in gold] == ["acme"] and gold[0]["score"] > first["score"]
    assert [h["key"] for h in graph.search("renewal", types=["person"])["hits"]] == ["7"]
    assert len(graph.search("renewal", limit=1)["hits"]) == 1
    expanded = graph.search("Dana", expand=True)["hits"][0]
    assert (expanded["out"][0]["key"], expanded["out_total"], expanded["in"]) == ("acme", 1, [])
    assert graph.search("nothing-like-it")["hits"] == []
    assert graph.search('"" ( ) *')["hits"] == []  # no words at all


def test_a_long_text_is_found_by_its_later_passages_and_one_hit_a_node(graph):
    long = "\n\n".join(f"Paragraph {n} mentions zebras." + " filler" * 150 for n in range(4))
    graph.upsert(nodes=[{"type": "doc", "key": "handbook", "title": "Handbook", "text": long}])
    hits = graph.search("zebras")["hits"]
    assert len(hits) == 1 and hits[0]["key"] == "handbook"
    assert graph.schema()["chunks"] > 3
    graph.upsert(nodes=[{"type": "doc", "key": "handbook", "text": "Short now."}])
    assert graph.search("zebras")["hits"] == [] and graph.schema()["chunks"] == 1


def test_neighbours_walk_out_in_and_by_relation(graph):
    seed(graph)
    graph.upsert(edges=[{"from": ACME, "rel": "in_region", "to": {"type": "region", "key": "emea"}}])
    one = graph.neighbours("deal", "d1")
    assert {(n["key"], n["distance"]) for n in one["nodes"]} == {("d1", 0), ("acme", 1)}
    assert one["edges"] == [{"from": DEAL, "rel": "belongs_to", "to": ACME}]
    two = graph.neighbours("deal", "d1", depth=2)
    assert {(n["key"], n["distance"]) for n in two["nodes"]} == {
        ("d1", 0),
        ("acme", 1),
        ("d2", 2),
        ("7", 2),
        ("emea", 2),
    }
    assert len(two["edges"]) == 4
    out = graph.neighbours("deal", "d1", direction="out", depth=3)
    assert {n["key"] for n in out["nodes"]} == {"d1", "acme", "emea"}
    assert {n["key"] for n in graph.neighbours("account", "acme", direction="in")["nodes"]} == {"acme", "d1", "d2", "7"}
    by_rel = graph.neighbours("account", "acme", rel="works_at")
    assert {n["key"] for n in by_rel["nodes"]} == {"acme", "7"}
    capped = graph.neighbours("account", "acme", limit=2)
    assert len(capped["nodes"]) == 3 and len(capped["edges"]) == 2
    assert graph.neighbours("region", "emea", direction="out", depth=3)["nodes"][0]["distance"] == 0
    with pytest.raises(NotFound, match="no region 'apac'"):
        graph.neighbours("region", "apac")


def test_nodes_are_listed_by_type_and_property(graph):
    seed(graph)
    assert [n["key"] for n in graph.nodes("deal")["nodes"]] == ["d2", "d1"]
    won = graph.nodes("deal", where={"stage": "won"})
    assert ([n["key"] for n in won["nodes"]], won["total"]) == (["d1"], 1)
    assert graph.nodes("account", where={"seats": 40, "active": True, "missing": None})["total"] == 1
    assert graph.nodes("account", where={"seats": 41})["total"] == 0
    paged = graph.nodes("deal", limit=1, offset=1)
    assert ([n["key"] for n in paged["nodes"]], paged["total"]) == (["d1"], 2)


def test_the_schema_names_types_properties_sources_and_relations(graph):
    seed(graph)
    schema = graph.schema()
    assert schema["types"] == [
        {"type": "account", "count": 1, "props": ["active", "seats", "tags", "tier"], "sources": ["crm_sync"]},
        {"type": "deal", "count": 2, "props": ["stage"], "sources": ["crm_sync"]},
        {"type": "person", "count": 1, "props": [], "sources": ["crm_sync"]},
    ]
    assert schema["relations"] == [
        {"from": "deal", "rel": "belongs_to", "to": "account", "count": 2},
        {"from": "person", "rel": "works_at", "to": "account", "count": 1},
    ]
    assert (schema["nodes"], schema["edges"]) == (4, 3)


def test_delete_takes_a_nodes_edges_and_words_with_it(graph):
    seed(graph)
    gone = graph.delete(
        nodes=[DEAL, {"type": "deal", "key": "never"}],
        edges=[
            {"from": {"type": "person", "key": "7"}, "rel": "works_at", "to": ACME},
            {"from": DEAL, "rel": "x", "to": DEAL},
        ],
    )
    assert gone == {"nodes": 1, "edges": 1}
    assert graph.node("account", "acme")["in_total"] == 1
    assert [h["key"] for h in graph.search("renewal")["hits"]] == ["7"]
    with pytest.raises(NotFound) as err:
        graph.node("deal", "d1")
    assert err.value.code == "E_GRAPH_NODE"
    with pytest.raises(BadRequest, match="An edge is"):
        graph.delete(edges=["deal:d1"])


@pytest.mark.parametrize(
    ("nodes", "edges", "why"),
    [
        ("x", None, "nodes is a list"),
        (["x"], None, "A node is named"),
        ([{"type": "Deal", "key": "1"}], None, "type is a name"),
        ([{"type": "deal", "key": ""}], None, "key is 1 to 256"),
        ([{"type": "deal", "key": "a\nb"}], None, "key is 1 to 256"),
        ([{"type": "deal", "key": True}], None, "key is 1 to 256"),
        ([{**DEAL, "colour": "red"}], None, "not colour"),
        ([{**DEAL, "title": 5}], None, "title is text"),
        ([{**DEAL, "text": "x" * (1024 * 1024 + 1)}], None, "text is text"),
        ([{**DEAL, "props": []}], None, "props is an object"),
        ([{**DEAL, "props": {"big": "x" * 70000}}], None, "over 64 KB"),
        (None, ["x"], "An edge is"),
        (None, [{"from": DEAL, "rel": "r", "to": ACME, "weight": 1}], "An edge is"),
        (None, [{"from": DEAL, "rel": "r", "to": "acme"}], "An edge's to is named"),
    ],
)
def test_a_misshapen_write_is_refused(graph, nodes, edges, why):
    with pytest.raises(BadRequest, match=why) as err:
        graph.upsert(nodes=nodes, edges=edges)
    assert err.value.code == "E_GRAPH"


@pytest.mark.parametrize(
    ("call", "why"),
    [
        (lambda g: g.search(""), "query is what to look for"),
        (lambda g: g.search(5), "query is what to look for"),
        (lambda g: g.search("x", types="deal"), "types is a list"),
        (lambda g: g.search("x", limit=0), "limit is a whole number from 1 to 50"),
        (lambda g: g.search("x", limit=True), "limit is a whole number"),
        (lambda g: g.nodes("deal", where=["stage"]), "where is"),
        (lambda g: g.nodes("deal", where={'a"b': 1}), "where is"),
        (lambda g: g.nodes("deal", where={"stage": ["won"]}), "where compares"),
        (lambda g: g.nodes("deal", offset=-1), "offset is a whole number from 0"),
        (lambda g: g.neighbours("deal", "d1", direction="up"), "direction is out, in or both"),
        (lambda g: g.neighbours("deal", "d1", depth=4), "depth is a whole number from 1 to 3"),
        (lambda g: g.neighbours("deal", "d1", rel="No"), "rel is a name"),
    ],
)
def test_a_misshapen_read_is_refused(graph, call, why):
    with pytest.raises(BadRequest, match=why):
        call(graph)


def test_text_is_cut_into_passages_at_paragraphs_and_spaces():
    assert chunks("") == [] and chunks("one\n\n\n\ntwo") == ["one\n\ntwo"]
    long = "word " * 500  # one paragraph of 2,500 characters
    cut = chunks(long)
    assert len(cut) == 3 and all(len(c) <= CHUNK for c in cut) and "".join(cut).count("word") == 500
    assert chunks("x" * 2500) == ["x" * 1200, "x" * 1200, "x" * 100]  # no space to cut at
    packed = chunks("a" * 700 + "\n\n" + "b" * 700 + "\n\n" + "c" * 100)
    assert [len(p) for p in packed] == [700, 802]
    assert chunks("short\n\n" + "y" * 1300)[0] == "short"


def test_a_nodes_words_and_a_querys_terms():
    assert words("T", {"a": "x", "n": 2, "flag": True, "list": [1]}, "body") == "T\n\na: x\nn: 2\n\nbody"
    assert words("", {}, "") == ""
    assert match('acme "OR" NEAR(acme') == '"acme" OR "or" OR "near"'
    assert match("...") is None
