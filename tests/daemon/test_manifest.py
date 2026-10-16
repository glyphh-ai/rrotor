import json
from pathlib import Path

import pytest

from rrotord import manifest as m
from rrotord.errors import BadRequest, DeployError


def src(tmp_path: Path, data: object, source: str = "def start(ctx, inputs):\n    pass\n") -> Path:
    (tmp_path / "glyphh.json").write_text(data if isinstance(data, str) else json.dumps(data))
    (tmp_path / "model.py").write_text(source)
    return tmp_path


def refusal(tmp_path: Path, data: object, source: str = "def start(ctx, inputs):\n    pass\n") -> str:
    with pytest.raises(DeployError) as caught:
        m.read(src(tmp_path, data, source))
    assert caught.value.code == "E_MANIFEST"
    return caught.value.message


def test_read_a_full_manifest(tmp_path):
    data = {
        "name": "daily_sales",
        "version": 2,
        "functions": {"start": {"inputs": {"type": "object"}, "approvals": ["write"]}},
        "hosts": ["api.example.com", "*.cdn.example.com"],
        "databases": ["postgres"],
        "limits": {"seconds": 60, "memory_mb": 256},
        "data": True,
    }
    found = m.read(src(tmp_path, data))
    assert found.name == "daily_sales" and found.limits.seconds == 60
    assert found.parts() == ["postgres"]


def test_a_minimal_manifest_needs_no_part(tmp_path):
    found = m.read(src(tmp_path, {"name": "p", "functions": {"start": {}}}))
    assert found.parts() == [] and found.limits.memory_mb == 512


def test_no_glyphh_json(tmp_path):
    with pytest.raises(DeployError, match=r"no glyphh\.json"):
        m.read(tmp_path)


def test_no_model_py(tmp_path):
    (tmp_path / "glyphh.json").write_text("{}")
    with pytest.raises(DeployError, match=r"no model\.py"):
        m.read(tmp_path)


def test_not_json(tmp_path):
    assert "glyphh.json is refused: Expecting value" in refusal(tmp_path, "nope")


def test_an_unknown_key(tmp_path):
    assert "extra: Extra inputs are not permitted" in refusal(
        tmp_path, {"name": "p", "functions": {"start": {}}, "extra": 1}
    )


def test_a_manifest_that_is_not_an_object(tmp_path):
    assert "glyphh.json:" in refusal(tmp_path, [1])


@pytest.mark.parametrize(
    ("data", "why"),
    [
        ({"name": "p", "functions": {}}, "at least one function"),
        ({"name": "p", "functions": {"load": {}}}, "load cannot be a function"),
        ({"name": "p", "functions": {"start": {}}, "hosts": ["nope"]}, "is not a host name"),
        ({"name": "p", "functions": {"start": {}}, "python": "3.12"}, "runs Python 3.13"),
        ({"name": "p", "functions": {"start": {"inputs": {"type": 5}}}}, "not a JSON Schema"),
    ],
)
def test_refused_manifests(tmp_path, data, why):
    assert why in refusal(tmp_path, data)


def test_a_function_missing_from_model_py(tmp_path):
    data = {"name": "p", "functions": {"start": {}, "other": {}}}
    assert refusal(tmp_path, data) == "model.py has no function other."


def test_model_py_that_does_not_parse(tmp_path):
    assert "does not parse: line 1" in refusal(tmp_path, {"name": "p", "functions": {"start": {}}}, "def (")


def test_check_inputs_passes_good_inputs():
    m.check_inputs(m.Function(inputs={"type": "object", "properties": {"a": {"type": "string"}}}), {"a": "x"})


def test_check_inputs_names_where_the_first_error_is():
    fn = m.Function(inputs={"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]})
    with pytest.raises(BadRequest) as caught:
        m.check_inputs(fn, {"a": 1})
    assert caught.value.code == "E_INPUTS" and caught.value.message.startswith("a: ")
    with pytest.raises(BadRequest, match=r"^inputs: "):
        m.check_inputs(fn, {})
