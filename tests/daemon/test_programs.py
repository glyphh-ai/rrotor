import pytest

from rrotord.errors import NotFound
from rrotord.manifest import Manifest
from rrotord.programs import Programs

SHA1, SHA2 = "1" * 40, "2" * 40


def manifest(name: str = "p", description: str = "") -> Manifest:
    return Manifest.model_validate({"name": name, "description": description, "functions": {"start": {}}})


def test_paths(tmp_path):
    p = Programs(tmp_path)
    assert p.folder("x") == tmp_path / "programs" / "x"
    assert p.files("x") == tmp_path / "programs" / "x" / "files"
    assert p.data("x") == tmp_path / "programs" / "x" / "data"


def test_nothing_deployed(tmp_path):
    p = Programs(tmp_path)
    assert (p.names(), p.versions("x"), p.deployed(), p.describe()) == ([], [], [], [])
    with pytest.raises(NotFound) as caught:
        p.get("x")
    assert caught.value.code == "E_PROGRAM_MISSING"


def test_record_activate_and_get(tmp_path):
    p = Programs(tmp_path)
    v1 = p.record("p", 1, SHA1, manifest(description="one"))
    assert (v1.src, v1.venv) == (v1.root / "src", v1.root / "venv")
    assert p.get("p", 1) == v1
    with pytest.raises(NotFound) as caught:
        p.get("p")
    assert caught.value.code == "E_VERSION_MISSING"
    p.activate(v1)
    assert p.get("p") == v1
    assert p.deployed() == [{"program": "p", "version": 1, "commit_sha": SHA1}]
    assert v1.to_json()["deployed_at"] == v1.deployed_at


def test_versions_are_in_number_order_and_skip_unrecorded_folders(tmp_path):
    p = Programs(tmp_path)
    p.record("p", 2, SHA1, manifest())
    p.record("p", 1, SHA2, manifest())
    p.version_root("p", "3" * 40).mkdir()
    assert [v.version for v in p.versions("p")] == [1, 2]
    assert p.next_number("p") == 3


def test_get_a_version_not_here(tmp_path):
    p = Programs(tmp_path)
    p.record("p", 1, SHA1, manifest())
    with pytest.raises(NotFound, match="no version 9"):
        p.get("p", 9)


def test_renumber(tmp_path):
    p = Programs(tmp_path)
    v = p.renumber(p.record("p", 1, SHA1, manifest()), 7)
    assert v.version == 7 and p.by_sha("p", SHA1) == v


def test_remove_the_active_version(tmp_path):
    p = Programs(tmp_path)
    v = p.record("p", 1, SHA1, manifest())
    p.activate(v)
    p.remove(v)
    assert p.active("p") is None and not v.root.exists()


def test_remove_another_version_keeps_the_active_one(tmp_path):
    p = Programs(tmp_path)
    v1, v2 = p.record("p", 1, SHA1, manifest()), p.record("p", 2, SHA2, manifest())
    p.activate(v1)
    p.remove(v2)
    assert p.active("p") == v1


def test_remove_with_nothing_active(tmp_path):
    p = Programs(tmp_path)
    v = p.record("p", 1, SHA1, manifest())
    p.remove(v)
    assert p.versions("p") == []


def test_describe_shows_the_active_or_the_latest(tmp_path):
    p = Programs(tmp_path)
    p.record("a", 1, SHA1, manifest("a", "first"))
    b = p.record("b", 1, SHA1, manifest("b", "old"))
    p.record("b", 2, SHA2, manifest("b", "new"))
    p.activate(b)
    p.version_root("c", SHA1).mkdir(parents=True)
    described = {d["name"]: d for d in p.describe()}
    assert described["a"]["description"] == "first" and described["a"]["active"] is None
    assert described["b"]["description"] == "old" and described["b"]["active"] == 1
    assert described["c"] == {"name": "c", "description": "", "active": None, "functions": {}, "versions": []}
    assert described["a"]["functions"] == {"start": {"description": "", "inputs": {"type": "object"}, "approvals": []}}
