"""An app's manifest (`"kind": "app"`, docs/architecture.md, 5.8): its app block checked, a static page needing no
program.py, the functions its page calls among its own, its roles lower-cased."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rrotord import manifest as m
from rrotord.errors import DeployError

PAGE = "<!doctype html><title>Board</title><p>hi</p>"


def write(tmp_path: Path, data: dict[str, Any], program: str | None = None, page: str | None = PAGE) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "glyphh.json").write_text(json.dumps(data))
    if program is not None:
        (tmp_path / "program.py").write_text(program)
    if page is not None:
        (tmp_path / data.get("app", {}).get("entry", "index.html")).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / data.get("app", {}).get("entry", "index.html")).write_text(page)
    return tmp_path


def app(**extra: object) -> dict[str, Any]:
    return {"name": "board", "kind": "app", "app": {"title": "The board", **extra}}


def test_a_static_app_needs_no_program_py_and_no_functions(tmp_path):
    found = m.read(write(tmp_path, app(roles=["SRE", "sre", "Admin"])))
    assert found.kind == "app" and found.static and found.functions == {}
    assert found.app is not None and found.app.roles == ["sre", "admin"] and found.app.entry == "index.html"
    assert found.app.allows({"member", "SRE"}) and not found.app.allows({"member"}) and found.app.allows({"ADMIN"})
    assert m.App(title="x").allows(set()) and m.App(title="x").allows({"anyone"})  # no roles: everyone


def test_an_app_with_functions_has_a_program_py_defining_them(tmp_path):
    data = app(functions=["rows"]) | {"functions": {"rows": {}, "other": {}}}
    found = m.read(write(tmp_path, data, "def rows(ctx, inputs):\n    pass\n\ndef other(ctx, inputs):\n    pass\n"))
    assert not found.static and found.app is not None and found.app.functions == ["rows"]
    with pytest.raises(DeployError, match=r"no program\.py"):
        m.read(write(tmp_path / "bare", data))
    with pytest.raises(DeployError, match="no function other"):
        m.read(write(tmp_path / "half", data, "def rows(ctx, inputs):\n    pass\n"))


@pytest.mark.parametrize(
    ("data", "why"),
    [
        ({"name": "board", "kind": "app"}, "has an app block"),
        (app(functions=["rows"]), "app.functions names rows, not among its functions"),
        ({"name": "p", "functions": {"start": {}}, "app": {"title": "x"}}, 'app is for a manifest of "kind": "app"'),
        (app(entry="../index.html"), "app.entry is a file inside src/"),
        (
            app(entry="page.txt"),
            "entry is a page (.html, .jsx, .tsx), a document (.md, .pdf), a diagram (.mmd, .svg) or an image",
        ),
        (app(type="slides"), "app.type"),
        (app(title=""), "app.title"),
        (app(roles=["bad role!"]), "app.roles.0"),
    ],
)
def test_refused_app_manifests(tmp_path, data, why):
    with pytest.raises(DeployError) as caught:
        m.read(write(tmp_path, data, "def rows(ctx, inputs):\n    pass\n"))
    assert caught.value.code == "E_MANIFEST" and why in caught.value.message


@pytest.mark.parametrize(
    ("entry", "given", "expected"),
    [
        ("index.html", None, "page"),
        ("a/b.HTM", None, "page"),
        ("app.tsx", None, "page"),
        ("app.jsx", "dashboard", "dashboard"),
        ("doc.md", None, "doc"),
        ("deck.pdf", None, "doc"),
        ("deck.pdf", "deck", "deck"),
        ("d.mmd", None, "diagram"),
        ("d.mermaid", None, "diagram"),
        ("d.svg", None, "diagram"),
        ("a.png", None, "design"),
        ("a.jpg", None, "design"),
        ("a.webp", "page", "page"),
    ],
)
def test_the_type_is_the_entrys_by_its_extension_unless_the_manifest_says(tmp_path, entry, given, expected):
    data = app(entry=entry) if given is None else app(entry=entry, type=given)
    found = m.read(write(tmp_path, data))
    assert found.app is not None and found.app.type == expected
    assert m.entry_type("x.txt") is None and m.entry_type("x.GIF") == "design"


def test_an_app_whose_page_is_missing_is_refused(tmp_path):
    with pytest.raises(DeployError, match=r"no pages/main\.md, the app's page"):
        m.read(write(tmp_path, app(entry="pages/main.md"), page=None))
    found = m.read(write(tmp_path / "md", app(entry="pages/main.md"), page="# Hi"))
    assert found.app is not None and found.app.entry == "pages/main.md"
