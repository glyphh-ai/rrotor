"""The manifest's `graph` (docs/architecture.md, 5.9): read or write, the head's alone in a cluster, never a CLI's,
and carried into an app's manifest when app_publish names it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rrotord import apps
from rrotord.manifest import Manifest
from tests.fakes import cli_manifest, manifest


def test_a_program_names_the_graph_to_read_or_to_write():
    assert Manifest.model_validate(manifest()).graph is None
    reader = Manifest.model_validate(manifest(graph="read"))
    writer = Manifest.model_validate(manifest(graph="write"))
    assert (reader.graph, writer.graph) == ("read", "write")
    assert reader.head_only and reader.writes("start")  # the graph is the head's, as the vault is
    assert not Manifest.model_validate(manifest()).head_only
    with pytest.raises(ValidationError, match="'read' or 'write'"):
        Manifest.model_validate(manifest(graph="all"))


def test_a_cli_has_no_graph():
    with pytest.raises(ValidationError, match="a cli runs its command, so it has no graph"):
        Manifest.model_validate(cli_manifest(graph="read"))


def test_an_app_published_with_graph_reads_it():
    data = {"title": "Deals", "graph": "read", "functions": {"won": {}}}
    made = apps.manifest_of(data, "deals", {"index.html": b"<p>hi</p>"})
    assert made["graph"] == "read" and Manifest.model_validate(made).graph == "read"
    assert "graph" not in apps.manifest_of({"title": "Deals"}, "deals", {"index.html": b"<p>hi</p>"})
