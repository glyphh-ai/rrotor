"""Deploying an Ada model: the spec checked with the manifest, and a changed encoding re-encoding the store through
the runner or refusing the deploy and leaving the version before active."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rrotor import ada
from rrotord import manifest as manifests
from rrotord.deploy import Deployer
from rrotord.errors import DeployError
from rrotord.models import Models
from tests.fakes import install

SPEC = {
    "name": "t",
    "seed": 1,
    "dimension": 1024,
    "layers": [{"name": "a", "segments": [{"name": "b", "roles": [{"name": "x"}]}]}],
}
MODEL = "from rrotor import Result\ndef start(ctx, inputs):\n    return Result.done()\n"


def adafest(**extra: Any) -> dict[str, Any]:
    return {"name": "t", "functions": {"start": {}}, "needs": ["ada"], "data": True, **extra}


def test_the_spec_is_required_checked_and_needs_the_data_folder(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    found = manifests.Manifest.model_validate({"name": "t", "functions": {"start": {}}})
    assert Deployer.ada_spec(src, found) is None
    with pytest.raises(DeployError, match='"data": true') as caught:
        Deployer.ada_spec(src, manifests.Manifest.model_validate(adafest(data=False)))
    assert caught.value.code == "E_MANIFEST"
    with pytest.raises(DeployError, match=r"beside model\.py"):
        Deployer.ada_spec(src, manifests.Manifest.model_validate(adafest()))
    (src / "spec.json").write_text(json.dumps(SPEC))
    assert Deployer.ada_spec(src, manifests.Manifest.model_validate(adafest())) == SPEC


async def test_an_unchanged_encoding_a_first_version_or_no_store_needs_no_reencode(tmp_path):
    models = Models(tmp_path)
    calls: list[str] = []

    async def maintain(found: Any, op: str) -> dict[str, Any]:
        calls.append(op)
        return {"status": "done", "data": {"reencoded": 1}, "step": None, "prompt": None}

    d = Deployer(models, tmp_path / "scratch", maintain=maintain)
    v1 = install(models, adafest(), MODEL, sha="a" * 40)
    (v1.src / "spec.json").write_text(json.dumps(SPEC))
    await d._ada_ready(v1, None)
    await d._ada_ready(v1, SPEC)  # the same commit as the active one
    v2 = install(models, adafest(), MODEL, sha="b" * 40, version=2)
    (v2.src / "spec.json").write_text(json.dumps(SPEC))
    models.activate(v1)
    await d._ada_ready(v2, SPEC)  # no store yet
    (models.data("t")).mkdir(parents=True, exist_ok=True)
    (models.data("t") / ada.STORE).write_bytes(b"")
    await d._ada_ready(v2, SPEC)  # the same encoding
    (v1.src / "spec.json").write_text("junk")
    await d._ada_ready(v2, {**SPEC, "seed": 9})  # the version before has no readable spec: nothing to compare
    assert calls == []
    (v1.src / "spec.json").write_text(json.dumps(SPEC))
    await d._ada_ready(v2, {**SPEC, "seed": 9})
    assert calls == [ada.REENCODE]


async def test_a_changed_encoding_that_cannot_be_reencoded_refuses_the_deploy(tmp_path):
    models = Models(tmp_path)
    v1 = install(models, adafest(), MODEL, sha="a" * 40)
    (v1.src / "spec.json").write_text(json.dumps(SPEC))
    models.data("t").mkdir(parents=True, exist_ok=True)
    (models.data("t") / ada.STORE).write_bytes(b"")
    v2 = install(models, adafest(), MODEL, sha="b" * 40, version=2)
    models.activate(v1)
    with pytest.raises(DeployError, match="cannot be re-encoded here") as caught:
        await Deployer(models, tmp_path / "s")._ada_ready(v2, {**SPEC, "seed": 9})
    assert caught.value.code == "E_ADA_ENCODING"

    async def failing(found: Any, op: str) -> dict[str, Any]:
        return {
            "status": "failed",
            "data": {"reason": "the file is not a database", "code": "E_CRASH"},
            "step": op,
            "prompt": None,
        }

    with pytest.raises(DeployError, match="not a database"):
        await Deployer(models, tmp_path / "s", maintain=failing)._ada_ready(v2, {**SPEC, "seed": 9})

    async def silent(found: Any, op: str) -> dict[str, Any]:
        return {"status": "failed"}

    with pytest.raises(DeployError, match="did not finish"):
        await Deployer(models, tmp_path / "s", maintain=silent)._ada_ready(v2, {**SPEC, "seed": 9})
