"""Apps over the API (docs/architecture.md, 5.8): published from Claude (REST and MCP), listed, served to a browser
(a local rrotor's own, or one signed in to Glyphh for a self-hosted one), gated by the catalogue's review and the
viewer's roles, and their pages' calls run as the viewer. The Glyphh server (the sign-in, the introspection) is faked
at the HTTP layer."""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord import apps
from rrotord.access import Access, digest
from rrotord.api import forward, ops
from rrotord.api.app import make
from rrotord.audit import Audit
from rrotord.browser import SESSION_COOKIE, Browser
from rrotord.catalogue import Catalogue
from rrotord.errors import BadRequest
from tests.daemon.api.conftest import Api, linked, services
from tests.daemon.api.test_hosted import Vouches
from tests.daemon.api.test_mcp import rpc, tool
from tests.fakes import install, manifest, token

PAGE = "<!doctype html><title>Board</title><script src='https://cdnjs.cloudflare.com/x.js'></script><p>hi</p>"
ROWS = """
from rrotor import Result

def rows(ctx, inputs):
    return Result.done({"rows": [inputs.get("n", 0) + 1], "who": ctx.caller})

def ask(ctx, inputs):
    if ctx.answer is None:
        return Result.needs_input("Which day?")
    return Result.done(ctx.answer)
"""
SELF_HOST = "rrotor.acme.com"
SERVER = "https://glyphh.example.com"


def publish_body(**over: Any) -> dict[str, Any]:
    return {"name": "board", "title": "The board", "description": "Hi there", "files": {"index.html": PAGE}, **over}


async def send(api: Api, method: str, path: str, **kw: Any) -> tuple[int, Any]:
    res = await api.client.request(method, path, headers=api.auth(), **kw)
    return res.status, await res.json()


def catalogue(api: Api) -> Catalogue:
    """The rrotor's word from the catalogue, which every rrotor here holds."""
    assert api.services.catalogue is not None
    return api.services.catalogue


@pytest.fixture
async def local(api: Api) -> Api:
    api.services.catalogue = Catalogue(api.services.config.data)
    return api


async def test_an_app_is_published_listed_and_served_on_a_local_rrotor(local: Api):
    link = linked(local)
    status, body = await send(
        local, "POST", "/v1/apps", json=publish_body(files={"index.html": PAGE, "a/b.css": "p{}"})
    )
    assert status == 201, body
    assert (body["program"], body["version"], body["active"], body["state"]) == ("board", 1, True, "unknown")
    assert (
        body["type"] == "page" and body["url"] == "http://127.0.0.1:7777/apps/board/" and len(body["commit_sha"]) == 40
    )
    [sent] = link.sent
    assert (sent["type"], sent["by"], sent["manifest"]["kind"], sent["manifest"]["app"]["title"]) == (
        "deployed",
        None,
        "app",
        "The board",
    )
    status, listed = await send(local, "GET", "/v1/apps")
    assert status == 200 and listed["apps"] == [
        {
            "name": "board",
            "title": "The board",
            "description": "Hi there",
            "type": "page",
            "version": 1,
            "active": True,
            "roles": [],
            "functions": [],
            "state": "unknown",
            "url": "http://127.0.0.1:7777/apps/board/",
        }
    ]
    res = await local.client.get("/apps/board/")  # no token: the person's own machine's browser
    assert res.status == 200 and res.content_type == "text/html" and await res.text() == PAGE
    assert (
        res.headers["Cache-Control"] == "no-store" and "cdnjs.cloudflare.com" in res.headers["Content-Security-Policy"]
    )
    res = await local.client.get("/apps/board/a/b.css")
    assert (res.status, res.content_type, await res.text()) == (200, "text/css", "p{}")
    for path in ("/apps/board/nope.css", "/apps/board/glyphh.json", "/apps/other/", "/apps/board/a/"):
        res = await local.client.get(path)
        assert res.status == 404 and res.headers["X-Error"] == "E_APP_MISSING" and "No such" in await res.text(), path
    assert "Sign out" not in await (await local.client.get("/apps/other/")).text()  # nobody signed in here
    install(local.services.programs, manifest("plain"), "def start(ctx, inputs):\n    pass\n", sha="e" * 40)
    assert (await local.client.get("/apps/plain/")).status == 404  # a program is not an app
    assert [a["name"] for a in (await send(local, "GET", "/v1/apps"))[1]["apps"]] == ["board"]  # nor listed
    status, again = await send(local, "POST", "/v1/apps", json=publish_body(files={"index.html": PAGE + "!"}))
    assert (status, again["version"], again["active"]) == (201, 2, True) and len(link.sent) == 2
    status, same = await send(local, "POST", "/v1/apps", json=publish_body(files={"index.html": PAGE + "!"}))
    assert (status, same["version"], same["active"]) == (201, 2, True) and len(link.sent) == 2  # held already


async def test_a_publish_is_an_audit_event_done_or_failed_and_a_forwarded_one_is_served(local: Api, tmp_path):
    local.services.audit = Audit(tmp_path / "audit", "rrotor-1", tier=lambda: "enterprise")
    body = publish_body(hosts=["api.example.com"], limits={"seconds": 30, "memory_mb": 128}, functions={"rows": {}})
    status, refused = await send(local, "POST", "/v1/apps", json=body)  # functions, but no program.py
    assert (status, refused["error"]["code"]) == (422, "E_MANIFEST")
    done = await forward.serve(
        local.services, {"op": "app_publish", "args": {"data": publish_body(hosts=["a.example"])}}
    )
    assert done["version"] == 1
    found = local.services.programs.get("board").manifest
    assert found.hosts == ["a.example"] and found.limits.seconds == 300
    kinds = [(e["kind"], e["outcome"]["status"]) for e in local.services.audit.read(None, 10)[0]]
    assert kinds == [("program.deploy_failed", "failed"), ("program.deploy", "done")]  # oldest first


async def test_a_markdown_document_is_served_in_the_document_shell(local: Api):
    files = {"notes/main.md": "# Hello\n\n<b>bold</b>", "notes/extra.md": "# Two"}
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(entry="notes/main.md", files=files))
    assert status == 201 and body["type"] == "doc", body
    res = await local.client.get("/apps/board/")
    text = await res.text()
    assert res.status == 200 and res.content_type == "text/html"
    assert "<title>The board</title>" in text and "# Hello\n\n&lt;b&gt;bold&lt;/b&gt;" in text and "marked" in text
    assert "# Two" in await (await local.client.get("/apps/board/notes/extra.md")).text()  # shelled by its path too


TSX = """import { useState } from "react";
import { LineChart } from "recharts";
type Row = { x: number };
export const rows: Row[] = [{ x: 1 }];
export default function App(): JSX.Element {
  const [n] = useState<number>(0);
  return <div className="p-4">{n}<LineChart data={rows} /></div>;
}
"""


async def test_a_react_page_is_served_in_the_react_shell(local: Api):
    files = {"app.tsx": TSX, "note.jsx": "export default () => <p>hi</p>;\n"}
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(entry="app.tsx", files=files))
    assert status == 201 and body["type"] == "page", body
    res = await local.client.get("/apps/board/")
    text = await res.text()
    assert res.status == 200 and res.content_type == "text/html" and "<title>The board</title>" in text
    assert (
        '<script type="importmap">' in text
        and '"recharts":"https://esm.sh/recharts@3?external=react,react-dom"' in text
    )
    assert '"react-dom/client":"https://esm.sh/react-dom@19/client?external=react"' in text
    assert '"react":"https://esm.sh/react@19"' in text and '"d3":"https://cdn.jsdelivr.net/npm/d3@7/+esm"' in text
    assert "babel-standalone" in text and "cdn.tailwindcss.com" in text
    assert '<script type="text/babel" data-type="module" data-presets="jsx,tsx">' in text
    assert (
        "\nfunction App(): JSX.Element {" in text
        and "\nconst __App = App;" in text
        and 'from "react-dom/client"' in text
    )
    assert "const rows: Row[]" in text and "export" not in text.split("</script>")[-2]
    res = await local.client.get("/apps/board/app.tsx")  # by its path: the file as it is
    assert (res.status, res.content_type, await res.text()) == (200, "text/plain", TSX)
    assert "esm.sh" in res.headers["Content-Security-Policy"]
    status, body = await send(
        local, "POST", "/v1/apps", json=publish_body(files={"note.jsx": "export default () => 1;\n"})
    )
    assert status == 201 and body["type"] == "page"  # the one file sent is the entry
    text = await (await local.client.get("/apps/board/")).text()
    assert 'data-presets="jsx">' in text and "\nconst __App = () => 1;\n" in text


def test_a_react_files_default_export_is_bound_for_the_mount():
    assert apps.mounted("export default function App() {}\n").startswith("function App() {}\n\nconst __App = App;\n")
    assert apps.mounted("export default class B extends C {}\n").startswith("class B extends C {}\n\nconst __App = B;")
    assert apps.mounted("export default async function* g() {}\n").startswith(
        "async function* g() {}\n\nconst __App = g;"
    )
    assert apps.mounted("export default memo(App);\n").startswith("const __App = memo(App);\n")
    assert apps.mounted("function A() {}\nexport { A as default };\n").startswith("function A() {}\nconst __App = A;\n")
    assert apps.mounted("export const a = 1;\nexport type T = 1;\nexport { a, b };\nexport default a;\n").startswith(
        "const a = 1;\ntype T = 1;\n\nconst __App = a;\n"
    )
    assert apps.mounted("export default () => 1;\nexport { x } from './y';\n").startswith("const __App = () => 1;\n\n")
    assert apps.mounted("const a = 1;\n").startswith("const a = 1;\n\nimport")  # no default: the browser says so
    assert apps.script_text('"</SCRIPT><!-- x"') == r'"<\/SCRIPT><\!-- x"'


async def test_a_diagram_is_served_in_the_diagram_shell_and_an_svg_as_it_is(local: Api):
    files = {"flow.mmd": "graph TD\n  A --> B & C\n"}
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(files=files))
    assert status == 201 and body["type"] == "diagram", body
    res = await local.client.get("/apps/board/")
    text = await res.text()
    assert res.status == 200 and res.content_type == "text/html"
    assert "mermaid@11" in text and '<pre class="mermaid">graph TD\n  A --&gt; B &amp; C\n</pre>' in text
    assert "<h1>The board</h1>" in text and "Content-Disposition" not in res.headers
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>'
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(files={"d.svg": svg}, type="diagram"))
    assert status == 201 and body["type"] == "diagram", body
    res = await local.client.get("/apps/board/")
    assert (res.status, res.content_type, await res.text()) == (200, "image/svg+xml", svg)
    assert res.headers["Content-Disposition"] == "inline"
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(files={"d.mermaid": "graph LR\n"}))
    assert (status, body["type"]) == (201, "diagram") and "mermaid@11" in await (
        await local.client.get("/apps/board/")
    ).text()


async def test_a_pdf_is_served_as_it_is_and_an_image_in_the_image_shell(local: Api):
    pdf = {"base64": base64.b64encode(b"%PDF-1.4 x").decode()}
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(files={"deck.pdf": pdf}, type="deck"))
    assert status == 201 and body["type"] == "deck", body
    res = await local.client.get("/apps/board/")
    assert (res.status, res.content_type, await res.read()) == (200, "application/pdf", b"%PDF-1.4 x")
    assert res.headers["Content-Disposition"] == "inline" and res.headers["X-Content-Type-Options"] == "nosniff"
    png = {"base64": base64.b64encode(b"\x89PNG").decode()}
    files = {"img/a-b.png": png, "readme.md": "# About"}
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(files=files, entry="img/a-b.png"))
    assert status == 201 and body["type"] == "design", body
    res = await local.client.get("/apps/board/")
    text = await res.text()
    assert res.status == 200 and res.content_type == "text/html" and "<h1>The board</h1>" in text
    assert '<img src="./img/a-b.png" alt="The board">' in text
    res = await local.client.get("/apps/board/img/a-b.png")
    assert (res.status, res.content_type, await res.read()) == (200, "image/png", b"\x89PNG")
    for name, kind in (
        ("x.jpg", "image/jpeg"),
        ("x.JPEG", "image/jpeg"),
        ("x.webp", "image/webp"),
        ("x.gif", "image/gif"),
    ):
        status, body = await send(local, "POST", "/v1/apps", json=publish_body(files={name: png}))
        assert (status, body["type"]) == (201, "design"), body
        assert "<img src=" in await (await local.client.get("/apps/board/")).text()
        assert (await local.client.get(f"/apps/board/{name}")).content_type == kind


@pytest.mark.parametrize(
    ("entry", "given", "expected"),
    [
        ("index.html", None, "page"),
        ("index.htm", "dashboard", "dashboard"),
        ("app.jsx", None, "page"),
        ("notes.md", None, "doc"),
        ("deck.pdf", None, "doc"),
        ("deck.pdf", "deck", "deck"),
        ("d.mmd", None, "diagram"),
        ("d.svg", "design", "design"),
        ("a.png", None, "design"),
    ],
)
async def test_the_type_is_the_entrys_unless_the_publisher_says(local: Api, entry, given, expected):
    over: dict[str, Any] = {"files": {entry: "x"}}
    if given is not None:
        over["type"] = given
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(**over))
    assert status == 201 and body["type"] == expected, body
    listed = (await send(local, "GET", "/v1/apps"))[1]["apps"][0]
    assert listed["type"] == expected
    kept = local.services.programs.get("board").manifest.app
    assert kept is not None and kept.type == expected  # the manifest keeps it


async def test_a_dashboard_is_never_inferred(local: Api):
    status, body = await send(local, "POST", "/v1/apps", json=publish_body())  # a page with functions is still a page
    assert status == 201 and body["type"] == "page"


async def test_a_new_version_of_an_approved_app_waits_for_its_review(local: Api):
    status, one = await send(local, "POST", "/v1/apps", json=publish_body())
    assert status == 201, one
    catalogue(local).set_one("board", 1, "approved", [])
    status, two = await send(local, "POST", "/v1/apps", json=publish_body(files={"index.html": "<p>two</p>"}))
    assert (status, two["version"], two["active"], two["state"]) == (201, 2, False, "draft")
    assert await (await local.client.get("/apps/board/")).text() == PAGE  # the approved version keeps serving
    status, listed = await send(local, "GET", "/v1/apps")
    assert (listed["apps"][0]["version"], listed["apps"][0]["state"]) == (1, "approved")
    assert (await local.client.get("/apps/board/?version=2")).status == 200  # the local browser may look at it
    assert await (await local.client.get("/apps/board/?version=2")).text() == "<p>two</p>"
    res = await local.client.get("/apps/board/?version=x")
    assert res.status == 400 and (await res.json())["error"]["code"] == "E_INPUTS"


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"name": "Board"}, "name is the app's program name"),
        ({"files": {}}, "files is an object"),
        ({"files": {f"f{i}.txt": "x" for i in range(65)}}, "At most 64 files"),
        ({"files": {"../x.html": "x"}}, "not a relative path"),
        ({"files": {"a/../x.html": "x"}}, "not a relative path"),
        ({"files": {"program.py": "x"}}, "rrotor's own"),
        ({"files": {"files/seed.txt": "x"}}, "rrotor's own"),
        ({"files": {"x.png": {"base64": "%%%"}}}, "base64 is not valid"),
        ({"files": {"x.png": 7}}, "a file is text, or {base64}"),
        ({"title": " "}, "Send title"),
        ({"description": 3}, "description is text"),
        ({"roles": "sre"}, "roles is a list"),
        ({"functions": {"rows": "x"}}, "functions is an object"),
        ({"functions": {"rows": {}}, "program": 3}, "program is the text of program.py"),
        (
            {"entry": "page.txt"},
            "entry is a page (.html, .jsx, .tsx), a document (.md, .pdf), a diagram (.mmd, .svg) or an image",
        ),
        ({"entry": 3}, "entry is a page"),
        ({"files": {"page.txt": "x"}}, "entry is a page"),  # the one file sent is the entry: not one that can be
        ({"files": {"page.txt": "x", "a.css": ""}}, "no index.html, the app's page"),
        ({"type": "slides"}, "type is one of page, dashboard, doc, diagram, deck, design"),
        ({"functions": {"rows": {}}}, "no program.py"),
        ({"files": {"other.html": PAGE, "a.css": ""}}, "no index.html, the app's page"),
    ],
)
async def test_a_publish_that_is_not_right_is_refused(local: Api, over, why):
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(**over))
    assert status in (400, 422) and why in body["error"]["message"], body
    assert local.services.programs.names() == []


async def test_files_past_sixteen_mib_are_refused(local: Api, monkeypatch):
    monkeypatch.setattr(apps, "BYTES_MAX", 10)
    status, body = await send(local, "POST", "/v1/apps", json=publish_body(files={"index.html": PAGE}))
    assert status == 400 and "over 16 MiB" in body["error"]["message"]
    raw = base64.b64encode(b"img").decode()
    assert apps.read_files({"x.png": {"base64": raw}}) == {"x.png": b"img"}
    with pytest.raises(BadRequest):
        apps.Apps.inputs_of(b"[1]")
    with pytest.raises(BadRequest):
        apps.Apps.inputs_of(b"{not json")


async def test_the_pages_calls_run_the_apps_functions_as_the_viewer(local: Api):
    functions = {"rows": {"description": "The rows"}, "ask": {}, "hidden": {}}
    program = ROWS + "\n\ndef hidden(ctx, inputs):\n    pass\n"
    body = publish_body(functions=functions, program=program)
    body["functions"] = functions
    status, made = await send(local, "POST", "/v1/apps", json=body)
    assert status == 201, made
    listed = (await send(local, "GET", "/v1/apps"))[1]["apps"][0]
    assert listed["functions"] == ["rows", "ask", "hidden"]
    res = await local.client.post("/apps/board/call/rows", data=b'{"n": 2}')
    answer = await res.json()
    assert res.status == 200 and (answer["status"], answer["data"]) == ("done", {"rows": [3], "who": "app"}), answer
    run = local.services.runner.get(answer["run_id"])
    assert (run.caller, run.origin, run.subject) == ("app", "app", None)
    res = await local.client.post("/apps/board/call/rows")  # no body: no inputs
    assert (await res.json())["data"]["rows"] == [1]
    res = await local.client.post("/apps/board/call/ask")
    waiting = await res.json()
    assert waiting["status"] == "needs_input"
    res = await local.client.post(f"/apps/board/runs/{waiting['run_id']}/continue", json={"answer": "monday"})
    assert (await res.json())["data"] == "monday"
    res = await local.client.post("/apps/board/call/nope", data=b"{}")
    assert res.status == 403 and (await res.json())["error"]["code"] == "E_APP_FUNCTION"
    res = await local.client.post("/apps/board/call/rows", data=b"[1]")
    assert res.status == 400
    install(local.services.programs, manifest("plain"), "def start(ctx, inputs):\n    pass\n", sha="e" * 40)
    status, other = await send(local, "POST", "/v1/runs", json={"program": "plain"})
    res = await local.client.post(f"/apps/board/runs/{other['run_id']}/continue", json={"answer": 1})
    assert res.status == 404 and (await res.json())["error"]["code"] == "E_RUN_MISSING"


async def test_apps_over_mcp(local: Api):
    arguments = publish_body(roles=["SRE"], type="dashboard")
    _, done = await rpc(local, "tools/call", {"name": "app_publish", "arguments": arguments})
    assert done["result"]["isError"] is False and done["result"]["structuredContent"]["version"] == 1
    assert done["result"]["structuredContent"]["type"] == "dashboard"
    listed = await tool(local, "apps_list")
    assert listed["structuredContent"]["apps"][0]["roles"] == ["sre"]
    assert listed["structuredContent"]["apps"][0]["type"] == "dashboard"
    _, refused = await rpc(local, "tools/call", {"name": "app_publish", "arguments": publish_body(name="Bad Name")})
    assert refused["result"]["isError"] and refused["result"]["structuredContent"]["error"]["code"] == "E_INPUTS"


async def test_a_rrotor_without_a_catalogue_keeps_no_apps(api: Api):
    assert api.services.catalogue is None
    for method, path, body in (("POST", "/v1/apps", publish_body()), ("GET", "/v1/apps", None)):
        status, answer = await send(api, method, path, json=body)
        assert (status, answer["error"]["code"]) == (503, "E_APPS"), path
    res = await api.client.get("/apps/board/")
    assert res.status == 503 and (await res.json())["error"]["code"] == "E_APPS"


class Glyphh:
    """The Glyphh server's OAuth endpoints as rrotor's browser sign-in calls them."""

    def __init__(self) -> None:
        self.tokens: list[dict[str, Any]] = []

    async def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        assert url.endswith("/oauth/register")
        return 201, {"client_id": "client-1"}

    async def post_form(self, url: str, headers: dict[str, str], form: dict[str, str]) -> tuple[int, Any]:
        assert url.endswith("/oauth/token")
        return 200, self.tokens.pop(0)


@dataclasses.dataclass
class Hosted:
    api: Api
    vouches: Vouches
    glyphh: Glyphh
    signed_in: str

    def session(self, access_token: str | None = None) -> dict[str, str]:
        """A browser session for this access token, as the callback would have made it: its cookie header."""
        browser = self.api.services.browser
        assert browser is not None
        made = browser._make(
            {"access_token": access_token or self.signed_in, "refresh_token": "rt", "expires_in": 3600}
        )
        return {"Cookie": f"{SESSION_COOKIE}={made.id}"}


async def open_self_hosted(tmp_path: Path, short_dir: Path, link: bool = True, **who: Any) -> Hosted:
    key, signed_in = token(), token()
    s = services(tmp_path, short_dir, token())
    s.config = dataclasses.replace(s.config, mode="self", public_host=SELF_HOST, local_token_sha256=None, link=link)
    s.access = Access(s.config.data, (digest(key),))
    vouches = Vouches(signed_in, **who)
    s.introspect = vouches  # type: ignore[assignment]
    s.catalogue = Catalogue(s.config.data, linked=link)
    glyphh = Glyphh()
    if link:
        s.browser = Browser(s.config.data, SERVER, s.config.address, "rrotor-1", glyphh.post_json, glyphh.post_form)
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(make(s)), headers={"Host": SELF_HOST})
    await client.start_server()
    return Hosted(Api(client, s, key, tmp_path), vouches, glyphh, signed_in)


@pytest.fixture
async def hosted(tmp_path: Path, short_dir: Path) -> AsyncIterator[Hosted]:
    h = await open_self_hosted(tmp_path, short_dir, role="member", groups=("SRE",))
    yield h
    await h.api.services.runner.shutdown()
    await h.api.client.close()


async def publish_as_person(h: Hosted, **over: Any) -> dict[str, Any]:
    res = await h.api.client.post(
        "/v1/apps", json=publish_body(**over), headers={"Authorization": f"Bearer {h.signed_in}"}
    )
    body: dict[str, Any] = await res.json()
    assert res.status == 201, body
    return body


async def test_publishing_is_a_persons_act_on_a_self_hosted_rrotor(hosted: Hosted):
    link = linked(hosted.api)
    res = await hosted.api.client.post("/v1/apps", json=publish_body(), headers=hosted.api.auth())
    assert res.status == 403 and (await res.json())["error"]["code"] == "E_FORBIDDEN"
    body = await publish_as_person(hosted)
    assert body["url"] == f"https://{SELF_HOST}/apps/board/" and link.sent[0]["by"] == "user_1"
    res = await hosted.api.client.get("/v1/apps", headers=hosted.api.auth())  # a key reads the list
    assert res.status == 200 and (await res.json())["apps"][0]["state"] == "unknown"


async def test_a_browser_signs_in_to_glyphh_and_opens_what_its_roles_and_the_review_allow(hosted: Hosted):
    await publish_as_person(hosted)
    client = hosted.api.client
    res = await client.get("/apps/board/", allow_redirects=False)
    assert res.status == 302 and res.headers["Location"] == "/auth/start?next=/apps/board/"
    res = await client.get("/auth/start?next=/apps/board/", allow_redirects=False)
    assert res.status == 302 and res.headers["Location"].startswith(f"{SERVER}/oauth/authorize?")
    state = parse_qs(urlsplit(res.headers["Location"]).query)["state"][0]
    signin = res.cookies["rrotor_signin"].value
    hosted.glyphh.tokens = [{"access_token": hosted.signed_in, "refresh_token": "rt", "expires_in": 3600}]
    res = await client.get(
        f"/auth/callback?code=c-1&state={state}", headers={"Cookie": f"rrotor_signin={signin}"}, allow_redirects=False
    )
    assert res.status == 302 and res.headers["Location"] == "/apps/board/"
    cookie = {"Cookie": f"{SESSION_COOKIE}={res.cookies[SESSION_COOKIE].value}"}
    res = await client.get("/apps/board/", headers=cookie)  # signed in, but nothing approved yet
    assert res.status == 409 and res.headers["X-Error"] == "E_APP_UNREVIEWED" and "Sign out" in await res.text()
    catalogue(hosted.api).set_one("board", 1, "approved", [])
    res = await client.get("/apps/board/", headers=cookie)
    assert res.status == 200 and await res.text() == PAGE
    assert hosted.vouches.asked == [hosted.signed_in] * 2 or hosted.signed_in in hosted.vouches.asked
    res = await client.get("/v1/apps", headers=cookie)  # the apps' own API takes the session too
    assert res.status == 200 and (await res.json())["apps"][0]["state"] == "approved"
    res = await client.get("/apps/board/", headers={**cookie, "Origin": "https://evil.example.com"})
    assert res.status == 403 and (await res.json())["error"]["code"] == "E_ORIGIN"
    res = await client.get("/apps/board/", headers={**cookie, "Origin": f"https://{SELF_HOST}"})
    assert res.status == 200
    catalogue(hosted.api).set_one("board", 1, "approved", ["ops"])  # the catalogue's roles are not the manifest's
    catalogue(hosted.api).set_one("board", 1, "rejected", [])
    res = await client.get("/apps/board/", headers=cookie)
    assert res.status == 409
    res = await client.post("/auth/logout", headers=cookie)
    assert res.status == 200 and "signed out" in await res.text()
    res = await client.get("/apps/board/", headers=cookie, allow_redirects=False)
    assert res.status == 302  # the session is gone: sign in again
    res = await client.post("/apps/board/call/rows", headers=cookie, data=b"{}")
    assert res.status == 401 and (await res.json())["error"]["code"] == "E_AUTH"
    res = await client.get("/v1/apps")
    assert res.status == 401


async def test_roles_a_reviewer_and_a_draft(hosted: Hosted):
    await publish_as_person(hosted, roles=["Ops"])
    client, word = hosted.api.client, catalogue(hosted.api)
    cookie = hosted.session()
    word.set_one("board", 1, "approved", ["ops"])
    res = await client.get("/apps/board/", headers=cookie)  # sre is not ops
    assert res.status == 403 and res.headers["X-Error"] == "E_APP_FORBIDDEN" and "Sign out" in await res.text()
    hosted.vouches.groups = ("ops", "SRE")  # the Cloud's word is asked for each request here: no minute kept
    res = await client.get("/apps/board/", headers=cookie)
    assert res.status == 200
    await publish_as_person(hosted, roles=["Ops"], files={"index.html": "<p>two</p>"})
    assert (await client.get("/apps/board/?version=2", headers=cookie)).status == 403
    hosted.vouches.role, hosted.vouches.groups = "admin", ()
    res = await client.get("/apps/board/?version=2", headers=cookie)
    assert res.status == 200 and await res.text() == "<p>two</p>"  # a reviewer looks at the draft
    assert (await client.get("/apps/board/", headers=cookie)).status == 200  # and the approved one
    res = await client.get("/apps/board/?version=9", headers=cookie)
    assert res.status == 404
    word.set_one("board", 2, "draft", ["ops"])  # nothing approved any more: a reviewer still opens it
    res = await client.get("/apps/board/", headers=cookie)
    assert res.status == 200
    hosted.vouches.role, hosted.vouches.groups = "member", ("Platform-Admins",)
    word.reviewers = ["platform-admins"]
    assert (await client.get("/apps/board/?version=2", headers=cookie)).status == 200


async def test_a_session_the_cloud_no_longer_vouches_for_is_dropped(hosted: Hosted):
    await publish_as_person(hosted)
    catalogue(hosted.api).set_one("board", 1, "approved", [])
    cookie = hosted.session("stale-" + "x" * 20)
    res = await hosted.api.client.get("/apps/board/", headers=cookie, allow_redirects=False)
    assert res.status == 302
    assert list((hosted.api.services.config.data / "sessions").iterdir()) == []


async def test_a_pages_call_runs_as_the_viewer(hosted: Hosted):
    await publish_as_person(hosted, functions={"rows": {}}, program=ROWS)
    s = hosted.api.services
    catalogue(hosted.api).set_one("board", 1, "approved", [])
    res = await hosted.api.client.post("/apps/board/call/rows", headers=hosted.session(), data=b'{"n": 1}')
    answer = await res.json()
    assert res.status == 200 and answer["data"]["rows"] == [2], answer
    run = s.runner.get(answer["run_id"])
    assert (run.caller, run.subject) == ("app", "user_1")


async def test_with_the_link_off_a_deploy_is_the_approval_and_there_is_no_sign_in(tmp_path, short_dir):
    h = await open_self_hosted(tmp_path, short_dir, link=False, role="member", groups=("SRE",))
    try:
        body = await publish_as_person(h, roles=["sre"])
        assert body["state"] == "unknown" and h.api.services.browser is None
        res = await h.api.client.get("/apps/board/", allow_redirects=False)
        assert res.status == 401  # no sign-in to send the browser to
        res = await h.api.client.get("/apps/board/", headers={"Authorization": f"Bearer {h.signed_in}"})
        assert res.status == 200 and await res.text() == PAGE  # a bearer sign-in opens it: the manifest's roles decide
        h.vouches.groups = ()
        h.api.services.introspect = Vouches(h.signed_in, role="member")  # type: ignore[assignment]
        res = await h.api.client.get("/apps/board/", headers={"Authorization": f"Bearer {h.signed_in}"})
        assert res.status == 403
    finally:
        await h.api.services.runner.shutdown()
        await h.api.client.close()


def test_what_is_served_and_what_is_not(tmp_path):
    assert apps.content_type("a/b.svg") == "image/svg+xml" and apps.content_type("x.bin") == "application/octet-stream"
    assert apps.content_type("x.png") == "image/png" and apps.content_type("x.PDF") == "application/pdf"
    assert (apps.content_type("x.woff2"), apps.content_type("x.csv"), apps.content_type("x.ico")) == (
        "font/woff2",
        "text/csv",
        "image/x-icon",
    )
    assert apps.content_type("x.md") == "text/markdown" and apps.content_type("noext") == "application/octet-stream"
    err = apps.AppError("x", code="E_OTHER", status=400)
    assert "Not served" in apps.refused_page(err, signed_in=False)


async def test_a_follower_sends_a_publish_to_its_head(local: Api, monkeypatch):
    seen: list[Any] = []

    async def relayed(s: Any, op: str, args: dict[str, Any], target: Any = None) -> dict[str, Any]:
        seen.append((op, args))
        return {"relayed": True}

    monkeypatch.setattr(forward, "follower", lambda s: True)
    monkeypatch.setattr(forward, "relayed", relayed)
    assert await ops.app_publish(local.services, publish_body()) == {"relayed": True}
    assert seen[0][0] == "app_publish"
