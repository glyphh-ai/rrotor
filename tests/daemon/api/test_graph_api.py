"""The graph over REST and MCP: written, searched, read, walked and described; a write audited with its counts, never
its content; refused when misshapen, by a rrotor that keeps none, and sent to the head by a follower. And the secrets
form: the tool that names the panel, the panel as an MCP App resource, its results never cached."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from rrotord.api import forward, mcp, panel
from rrotord.audit import Audit
from tests.daemon.api.test_mcp import META, rpc, tool
from tests.daemon.api.test_routes import send
from tests.fakes import install, manifest

ACME = {"type": "account", "key": "acme"}
DEAL = {"type": "deal", "key": "d1"}
NODES = [
    {**ACME, "title": "Acme Corp", "props": {"tier": "gold"}},
    {**DEAL, "title": "Renewal 2027", "props": {"stage": "won"}, "text": "The renewal closed in March."},
]
EDGES = [{"from": DEAL, "rel": "belongs_to", "to": ACME}]
READS = "from rrotor import Result\n\ndef start(ctx, inputs):\n    return Result.done(len(ctx.secrets['API_KEY']))\n"


async def test_the_graph_over_rest(api, tmp_path):
    api.services.audit = Audit(tmp_path / "audit", "n1", tier=lambda: "enterprise")
    status, empty = await send(api, "GET", "/v1/graph")
    assert (status, empty) == (200, {"types": [], "relations": [], "nodes": 0, "edges": 0, "chunks": 0})
    status, wrote = await send(api, "POST", "/v1/graph/upsert", json={"nodes": NODES, "edges": EDGES})
    assert (status, wrote) == (200, {"nodes": 2, "edges": 1})
    status, found = await send(api, "POST", "/v1/graph/search", json={"query": "renewal", "expand": True})
    assert [h["key"] for h in found["hits"]] == ["d1"] and found["hits"][0]["out"][0]["key"] == "acme"
    status, node = await send(api, "POST", "/v1/graph/node", json=ACME)
    assert (node["title"], node["in_total"], node["source"]) == ("Acme Corp", 1, "")
    status, listed = await send(api, "POST", "/v1/graph/nodes", json={"type": "deal", "where": {"stage": "won"}})
    assert listed["total"] == 1
    status, near = await send(api, "POST", "/v1/graph/neighbours", json={**ACME, "direction": "in"})
    assert {n["key"] for n in near["nodes"]} == {"acme", "d1"}
    status, schema = await send(api, "GET", "/v1/graph")
    assert schema["relations"] == [{"from": "deal", "rel": "belongs_to", "to": "account", "count": 1}]
    status, gone = await send(api, "POST", "/v1/graph/delete", json={"nodes": [DEAL]})
    assert gone == {"nodes": 1, "edges": 0}
    events = [e for e in api.services.audit.query({"limit": "50"})["events"] if e["kind"].startswith("graph.")]
    assert [(e["kind"], e["nodes"], e["edges"]) for e in events] == [("graph.delete", 1, 0), ("graph.upsert", 2, 1)]
    assert "Renewal" not in str(events)


async def test_refusals(api):
    status, body = await send(api, "POST", "/v1/graph/search", json={"query": ""})
    assert (status, body["error"]["code"]) == (400, "E_GRAPH")
    status, body = await send(api, "POST", "/v1/graph/explode", json={})
    assert (status, body["error"]["code"]) == (400, "E_GRAPH") and "upsert" in body["error"]["message"]
    status, body = await send(api, "POST", "/v1/graph/node", json=ACME)
    assert (status, body["error"]["code"]) == (404, "E_GRAPH_NODE")
    graph = api.services.runner.graph
    api.services.runner.graph = None
    status, body = await send(api, "GET", "/v1/graph")
    assert (status, body["error"]["code"]) == (503, "E_GRAPH")
    graph.close()


async def test_a_follower_sends_the_graph_to_its_head_which_does_it(api):
    asked: list[tuple[str, dict[str, Any]]] = []

    async def relay(op: str, args: dict[str, Any], target: str | None = None) -> dict[str, Any]:
        asked.append((op, args))
        return {"result": {"nodes": 2, "edges": 1}}

    api.services.cluster = cast(Any, SimpleNamespace(role="follower", member=True, state={"head": "n0"}, forward=relay))
    status, wrote = await send(api, "POST", "/v1/graph/upsert", json={"nodes": NODES, "edges": EDGES})
    assert (status, wrote) == (200, {"nodes": 2, "edges": 1})
    assert asked == [("graph", {"graph": "upsert", "data": {"nodes": NODES, "edges": EDGES}})]
    assert api.services.runner.graph.schema()["nodes"] == 0  # nothing was written here
    api.services.cluster = None
    done = await forward._serve(api.services, "graph", asked[0][1])
    assert done == {"nodes": 2, "edges": 1} and api.services.runner.graph.schema()["nodes"] == 2


async def test_the_mcp_tools(api):
    wrote = await tool(api, "graph_upsert", nodes=NODES, edges=EDGES)
    assert wrote["isError"] is False and wrote["structuredContent"] == {"nodes": 2, "edges": 1}
    hits = (await tool(api, "graph_search", query="march renewal", types=["deal"], limit=5))["structuredContent"]
    assert hits["hits"][0]["passage"].endswith("The renewal closed in March.")
    assert (await tool(api, "graph_node", **ACME))["structuredContent"]["title"] == "Acme Corp"
    assert (await tool(api, "graph_nodes", type="deal"))["structuredContent"]["total"] == 1
    near = (await tool(api, "graph_neighbours", **DEAL, depth=2))["structuredContent"]
    assert near["edges"] == EDGES
    assert (await tool(api, "graph_schema"))["structuredContent"]["nodes"] == 2
    assert (await tool(api, "graph_delete", edges=EDGES))["structuredContent"] == {"nodes": 0, "edges": 1}
    refused = await tool(api, "graph_node", type="account", key="nope")
    assert refused["isError"] is True and refused["structuredContent"]["error"]["code"] == "E_GRAPH_NODE"
    listed = {t["name"]: t for t in (await rpc(api, "tools/list"))[1]["result"]["tools"]}
    read_only = {n for n, t in listed.items() if n.startswith("graph_") and t["annotations"]["readOnlyHint"]}
    assert read_only == {"graph_schema", "graph_search", "graph_node", "graph_nodes", "graph_neighbours"}
    assert {"graph_upsert", "graph_delete"} <= set(listed)


async def test_the_secrets_form_names_its_panel_and_never_a_value(api):
    install(api.services.programs, manifest("sample", secrets=["API_KEY", "OTHER"]), READS)
    api.services.runner.vault.set("sample", "OTHER", "sk-live-0123456789")
    shown = await tool(api, "secrets_form", program="sample")
    got = shown["structuredContent"]
    assert got["secrets"] == [
        {"name": "API_KEY", "declared": True, "set": False},
        {"name": "OTHER", "declared": True, "set": True},
    ]
    assert got["unset"] == ["API_KEY"] and "never into the chat" in got["how"]
    assert "http://127.0.0.1:7777/v1/programs/sample/secrets/<NAME>" in got["how"] and "sk-live" not in str(shown)
    listed = {t["name"]: t for t in (await rpc(api, "tools/list"))[1]["result"]["tools"]}
    assert listed["secrets_form"]["_meta"] == {"ui": {"resourceUri": panel.SECRETS_URI}}
    assert "_meta" not in listed["secrets_list"] and listed["secrets_form"]["annotations"]["readOnlyHint"] is True
    missing = await tool(api, "secrets_form", program="nothing")
    assert missing["structuredContent"]["error"]["code"] == "E_PROGRAM_MISSING"


async def test_the_panel_is_an_mcp_app_resource(api):
    capabilities = (await rpc(api, "server/discover", META))[1]["result"]["capabilities"]
    assert capabilities == {"tools": {}, "resources": {}}
    assert (await rpc(api, "initialize", {"protocolVersion": "2025-06-18"}))[1]["result"][
        "capabilities"
    ] == capabilities
    resources = (await rpc(api, "resources/list"))[1]["result"]
    assert resources == {"resources": list(panel.RESOURCES)} and resources["resources"][0]["mimeType"] == panel.MIME
    assert (await rpc(api, "resources/templates/list"))[1]["result"] == {"resourceTemplates": []}
    read = (await rpc(api, "resources/read", {"uri": panel.SECRETS_URI}))[1]["result"]["contents"][0]
    assert (read["uri"], read["mimeType"], read["_meta"]) == (
        panel.SECRETS_URI,
        panel.MIME,
        {"ui": {"prefersBorder": True}},
    )
    page = read["text"]
    assert "__APP_JS__" not in page and "window.McpApp=" in page and "secret_set" in page
    assert "</script>" in page and page.count("<script") == page.count("</script>")  # the bundle's own are escaped
    refused = (await rpc(api, "resources/read", {"uri": "ui://rrotor/nothing/1"}))[1]
    assert refused["error"]["code"] == mcp.INVALID_PARAMS


async def test_a_2026_07_28_client_is_told_never_to_cache_lists_and_reads(api):
    for method, params in (
        ("tools/list", {}),
        ("resources/list", {}),
        ("resources/templates/list", {}),
        ("resources/read", {"uri": panel.SECRETS_URI}),
    ):
        result = (await rpc(api, method, {**params, **META}))[1]["result"]
        assert (result["ttlMs"], result["cacheScope"], result["resultType"]) == (0, "private", "complete"), method
    assert "ttlMs" not in (await rpc(api, "ping", META))[1]["result"]
    assert "ttlMs" not in (await rpc(api, "tools/list"))[1]["result"]  # the earlier revisions carry none
