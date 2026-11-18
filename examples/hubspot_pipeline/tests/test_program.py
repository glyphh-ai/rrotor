"""hubspot_pipeline through rrotor.testing: HubSpot answered by a fake, the graph kept in memory."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

from rrotor.http import Response
from rrotor.testing import FakeGraph, FakeHttp, Harness

HERE = Path(__file__).resolve().parent.parent
API = "https://api.hubapi.com/crm/v3/objects"
COMPANIES = [
    {"id": "100", "properties": {"name": "Acme Corp", "domain": "acme.com", "industry": "", "description": None}},
    {"id": "101", "properties": {"name": None, "domain": "globex.com"}},
]
CONTACTS = [
    {
        "id": "200",
        "properties": {"firstname": "Dana", "lastname": "Ruiz", "email": "dana@acme.com", "jobtitle": "CFO"},
        "associations": {"companies": {"results": [{"id": "100", "type": "contact_to_company"}]}},
    },
    {"id": "201", "properties": {"email": "sam@globex.com"}},
]
DEALS = [
    {
        "id": "300",
        "properties": {
            "dealname": "Renewal 2027",
            "amount": "48000",
            "dealstage": "closedwon",
            "description": "Three years.",
        },
        "associations": {
            "companies": {"results": [{"id": "100", "type": "deal_to_company"}, {"id": "100", "type": "primary"}]},
            "contacts": {"results": [{"id": "200", "type": "deal_to_contact"}]},
        },
    }
]


def program() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hubspot_pipeline_program", HERE / "program.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hubspot(method: str, url: str, body: bytes | None) -> Response:
    """Companies come in two pages; the rest in one."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    objects = parts.path.rsplit("/", 1)[-1]
    if objects == "companies" and "after" not in query:
        page = {"results": COMPANIES[:1], "paging": {"next": {"after": "100"}}}
    elif objects == "companies":
        page = {"results": COMPANIES[1:]}
    else:
        page = {"results": {"contacts": CONTACTS, "deals": DEALS}[objects]}
    return Response(200, json.dumps(page).encode(), {"Content-Type": "application/json"})


def harness(tmp_path: Path, http: FakeHttp | None = None) -> Harness:
    return Harness(
        program(),
        tmp_path,
        http=http or FakeHttp({API: hubspot}),
        secrets={"HUBSPOT_TOKEN": "pat-test"},
        graph=FakeGraph("write"),
    )


def test_a_sync_writes_companies_contacts_and_deals_with_their_edges(tmp_path: Path) -> None:
    h = harness(tmp_path)
    done = h.call("sync")
    assert done.status == "done", done.data
    assert done.data == {
        "company": {"nodes": 2, "edges": 0},
        "contact": {"nodes": 2, "edges": 1},
        "deal": {"nodes": 1, "edges": 2},
    }
    nodes, edges = h.graph.nodes, h.graph.edges
    assert nodes[("company", "100")] == {
        "title": "Acme Corp",
        "props": {"name": "Acme Corp", "domain": "acme.com"},
        "text": "",
    }
    assert nodes[("company", "101")]["title"] == "globex.com"
    assert (nodes[("contact", "200")]["title"], nodes[("contact", "201")]["title"]) == ("Dana Ruiz", "sam@globex.com")
    deal = nodes[("deal", "300")]
    assert (deal["title"], deal["text"], deal["props"]["dealstage"]) == ("Renewal 2027", "Three years.", "closedwon")
    assert set(edges) == {
        (("contact", "200"), "works_at", ("company", "100")),
        (("deal", "300"), "belongs_to", ("company", "100")),
        (("deal", "300"), "involves", ("contact", "200")),
    }
    asked = [(urlsplit(url).path.rsplit("/", 1)[-1], parse_qs(urlsplit(url).query)) for _, url, _ in h.http.calls]
    assert [objects for objects, _ in asked] == ["companies", "companies", "contacts", "deals"]
    assert asked[1][1]["after"] == ["100"] and asked[3][1]["associations"] == ["companies,contacts"]
    assert "associations" not in asked[0][1] and asked[0][1]["limit"] == ["100"]
    assert [line["detail"]["type"] for line in h.logs] == ["company", "contact", "deal"]


def test_running_it_again_keeps_one_of_each(tmp_path: Path) -> None:
    h = harness(tmp_path)
    h.call("sync")
    h.call("sync")
    assert len(h.graph.nodes) == 5 and len(h.graph.edges) == 3


def test_hubspot_refusing_the_token_is_a_failed_result(tmp_path: Path) -> None:
    refused = harness(tmp_path, FakeHttp({API: Response(401, b'{"message": "bad token"}')})).call("sync")
    assert refused.status == "failed" and refused.data["code"] == "E_HTTP"
