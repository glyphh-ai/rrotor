"""hubspot_pipeline: a pipeline into the rrotor's graph (docs/architecture.md, 5.9).

Three steps, each a plain function, run in order by `sync`: **extract** reads a kind of record from HubSpot's CRM API a
page at a time (its one declared host, with the token the person set as the secret HUBSPOT_TOKEN); **transform** makes
each record a flat row; **store** writes the rows to the graph with `ctx.graph.rows`, a node a record and an edge for
each record it is associated with (not `load`: rrotor calls a program's own `load(ctx)` before every run). Run again,
it writes the same nodes again: the graph keeps one of each."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from rrotor import Ctx, Result

API = "https://api.hubapi.com/crm/v3/objects"
PAGE = 100

# Each kind of record: the node type it becomes, the properties read, how it is titled, the text it is found by, and
# the records it is associated with (the association read, the edge's relation, the node type at its other end).
KINDS: dict[str, dict[str, Any]] = {
    "companies": {
        "type": "company",
        "properties": ["name", "domain", "industry", "city", "country", "description"],
        "title": lambda row: row.get("name") or row.get("domain") or row["id"],
        "text": ["description"],
        "links": {},
    },
    "contacts": {
        "type": "contact",
        "properties": ["firstname", "lastname", "email", "jobtitle"],
        "title": lambda row: " ".join(p for p in (row.get("firstname"), row.get("lastname")) if p) or row.get("email"),
        "text": [],
        "links": {"companies": ("works_at", "company")},
    },
    "deals": {
        "type": "deal",
        "properties": ["dealname", "amount", "dealstage", "closedate", "description"],
        "title": lambda row: row.get("dealname") or row["id"],
        "text": ["description"],
        "links": {"companies": ("belongs_to", "company"), "contacts": ("involves", "contact")},
    },
}


def extract(ctx: Ctx, objects: str, kind: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every record of a kind, a page at a time, with the properties and associations the kind names."""
    headers = {"Authorization": f"Bearer {ctx.secrets['HUBSPOT_TOKEN']}"}
    after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": PAGE, "properties": ",".join(kind["properties"])}
        if kind["links"]:
            params["associations"] = ",".join(kind["links"])
        if after:
            params["after"] = after
        page = ctx.http.get(f"{API}/{objects}", params=params, headers=headers).raise_for_status().json()
        yield from page.get("results", [])
        after = ((page.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            return


def transform(record: dict[str, Any], kind: dict[str, Any]) -> dict[str, Any]:
    """A record as a flat row: its id, its properties that have a value, and the ids of what it is associated with."""
    row: dict[str, Any] = {"id": record["id"]}
    row.update({k: v for k, v in (record.get("properties") or {}).items() if v not in (None, "")})
    for name in kind["links"]:
        linked = ((record.get("associations") or {}).get(name) or {}).get("results") or []
        row[name] = sorted({str(a["id"]) for a in linked})
    return row


def store(ctx: Ctx, rows: list[dict[str, Any]], kind: dict[str, Any]) -> dict[str, int]:
    """The rows written to the graph: a node each, an edge to each record it is associated with."""
    return ctx.graph.rows(
        rows, type=kind["type"], key="id", title=kind["title"], text=kind["text"], links=kind["links"]
    )


def sync(ctx: Ctx, inputs: dict[str, Any]) -> Result:
    wrote: dict[str, dict[str, int]] = {}
    for objects, kind in KINDS.items():
        rows = [transform(record, kind) for record in extract(ctx, objects, kind)]
        wrote[kind["type"]] = store(ctx, rows, kind)
        ctx.log("stored", type=kind["type"], **wrote[kind["type"]])
    return Result.done(wrote)
