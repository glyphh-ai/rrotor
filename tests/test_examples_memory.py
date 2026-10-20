"""Each Ada example's `limits.memory_mb` holds a run on a store at Community's precedent cap: a model a customer
copies must reach the cap it is sold with, not crash with MemoryError on the way (at 512 MB a 4096-dimension store
failed every run, `query` included, from 4,096 precedents). The run is the sandbox's: the data limit the manifest
sets (`prlimit --data`), BLAS on one thread and two malloc arenas, the kernel the repo's tests carry."""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest

from rrotord.licence_key import COMMUNITY

ROOT = Path(__file__).parent.parent
KERNEL = ROOT / "tests" / "kernel"
ADA = sorted(
    p.parent for p in (ROOT / "examples").glob("*/glyphh.json") if "ada" in json.loads(p.read_text()).get("needs", [])
)
SANDBOX_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "MALLOC_ARENA_MAX": "2"}

CHILD = r"""
import json, sys
import glyphh_spec

mode, spec_path, store, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
raw = json.load(open(spec_path))
model = glyphh_spec.Model(glyphh_spec.Spec.from_json(raw), author="t", path=store)

def case(i):
    data = {}
    for layer in raw["layers"]:
        for segment in layer["segments"]:
            for role in segment["roles"]:
                name = role["name"]
                if role.get("key_part"):
                    value = f"id-{i}"
                elif f"{layer['name']}.{segment['name']}.{name}" == raw.get("temporal_source"):
                    value = 1_790_000_000 + 3600 * i
                elif "numeric_config" in role:
                    value = role["numeric_config"]["min_value"] + i % 5
                elif role.get("text_encoding"):
                    value = f"word{i % 50} part{i % 13} thing{i % 7}"
                else:
                    value = f"v{i % 5}"
                data.setdefault(layer["name"], {}).setdefault(segment["name"], {})[name] = value
    return data

if mode == "fill":
    for i in range(n):
        model.record(case(i), "a" if i % 2 else "b")
else:
    assert len(model) == n, len(model)
    model.query(case(7))
    model.record(case(0), "a")  # replaces a row: the store stays at the cap
print(len(model))
"""


def child(mode: str, example: Path, store: Path, n: int, memory_mb: int | None) -> subprocess.CompletedProcess[str]:
    def limit() -> None:
        if memory_mb is not None:
            data = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_DATA, (data, data))

    env = {**os.environ, **SANDBOX_ENV, "PYTHONPATH": str(KERNEL)}
    return subprocess.run(
        [sys.executable, "-c", CHILD, mode, str(example / "spec.json"), str(store), str(n)],
        env=env,
        preexec_fn=limit,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_DATA is the sandbox's limit only on Linux")
@pytest.mark.parametrize("example", ADA, ids=lambda p: p.name)
def test_an_ada_examples_memory_holds_a_run_at_the_community_cap(example: Path, tmp_path: Path) -> None:
    cap = COMMUNITY.max_precedents
    assert cap is not None
    memory_mb = json.loads((example / "glyphh.json").read_text())["limits"]["memory_mb"]
    store = tmp_path / "precedents.sqlite"
    filled = child("fill", example, store, cap, None)
    assert filled.returncode == 0, filled.stderr[-2000:]
    run = child("run", example, store, cap, memory_mb)
    assert run.returncode == 0, f"{example.name} at {memory_mb} MB: {run.stderr[-600:]}"
    assert run.stdout.strip() == str(cap)
