"""A CLI's manifest (`"kind": "cli"`): its cli block checked, refused with clear reasons, and the examples' read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rrotord import manifest as m
from rrotord.errors import DeployError
from tests.fakes import cli_manifest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
SHA = "a" * 64


def refusal(tmp_path: Path, data: dict[str, Any]) -> str:
    (tmp_path / "glyphh.json").write_text(json.dumps(data))
    (tmp_path / "program.py").write_text("def start(ctx, inputs):\n    pass\n")  # a CLI ignores it
    with pytest.raises(DeployError) as caught:
        m.read(tmp_path)
    assert caught.value.code == "E_MANIFEST"
    return caught.value.message


def test_a_cli_needs_no_program_py_and_runs_on_the_head(tmp_path):
    (tmp_path / "glyphh.json").write_text(json.dumps(cli_manifest(secrets=["TOKEN"], hosts=["api.example.com"])))
    found = m.read(tmp_path)
    assert (found.kind, found.functions, found.head_only, found.writes("anything")) == ("cli", {}, True, True)
    assert found.cli is not None and found.cli.install.source == "url" and found.cli.secret_refs() == {}


@pytest.mark.parametrize(
    ("install", "source", "hosts"),
    [
        ({"url": "https://example.com/t.tgz", "sha256": SHA}, "url", {"example.com"}),
        (
            {"url": "https://github.com/o/r/releases/download/v1/t.tgz", "sha256": SHA},
            "url",
            {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"},
        ),
        ({"npm": "@scope/tool@1.2.3", "sha256": SHA}, "npm", {"registry.npmjs.org"}),
        ({"pypi": "glyphh==3.5.0", "sha256": SHA}, "pypi", {"pypi.org", "files.pythonhosted.org"}),
        ({"image": "git"}, "image", set()),
    ],
)
def test_each_install_source_and_the_hosts_it_implies(install, source, hosts):
    command = "git" if source == "image" else "tool"
    found = m.Manifest.model_validate(cli_manifest(cli={"install": install, "command": command}))
    assert found.cli is not None and found.cli.install.source == source
    assert found.cli.install.implied_hosts() == hosts
    assert found.cli.install.pinned == next(v for k, v in install.items() if k != "sha256")


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"cli": None}, "has a cli block"),
        ({"functions": {"start": {}}}, "it has no functions"),
        ({"data": True, "databases": ["sqlite"]}, "it has no data, databases"),
        ({"cli_install": {}}, "install names one source"),
        ({"cli_install": {"url": "https://a.com/x", "npm": "a@1.0.0", "sha256": SHA}}, "install names one source"),
        ({"cli_install": {"url": "https://a.com/x"}}, "install from url needs its sha256"),
        ({"cli_install": {"url": "http://a.com/x", "sha256": SHA}}, "https:// address on port 443"),
        ({"cli_install": {"url": "https://u:p@a.com/x", "sha256": SHA}}, "https:// address on port 443"),
        ({"cli_install": {"url": "https://a.com:8443/x", "sha256": SHA}}, "https:// address on port 443"),
        ({"cli_install": {"npm": "tool@^1.0.0", "sha256": SHA}}, "an exact version"),
        ({"cli_install": {"pypi": "tool>=1", "sha256": SHA}}, "an exact version"),
        ({"cli_install": {"image": "/usr/../etc/x"}}, "path under /usr"),
        ({"cli_install": {"image": "/opt/x"}}, "path under /usr"),
        ({"cli_install": {"url": "https://a.com/x", "sha256": "ABC"}}, "String should match pattern"),
        ({"cli_install": {"image": "git"}}, "command is the image binary's name, git"),
        ({"cli_command": "../x"}, "relative path inside the install"),
        ({"cli_command": "/bin/sh"}, "relative path inside the install"),
        ({"cli_interactive": True}, "interactive sessions are not built yet"),
        ({"cli_env": {"PATH": "/x"}}, "PATH is set by rrotor"),
        ({"cli_env": {"https_proxy": "x"}}, "set by rrotor"),
        ({"cli_env": {"LD_PRELOAD": "x"}}, "set by rrotor"),
        ({"cli_env": {"T": "secret:lower"}}, "secret:NAME (or secret?:NAME) names a secret"),
        ({"cli_env": {"T": "secret?:"}}, "names a secret"),
        ({"cli_env": {"T": "secret:TOKEN"}}, "takes the secret TOKEN, not in secrets"),
        ({"cli_login": {"args": ["login"]}}, "set home to true"),
        ({"cli_home": True, "cli_login": {"args": ["login"], "env": {"T": "secret:K"}}}, "the secret K, not in"),
        ({"cli_home": True, "cli_login": {"args": [], "env": {"HOME": "/x"}}}, "HOME is set by rrotor"),
        ({"cli_home": True, "cli_login": {"args": [], "code": "("}}, "not a regular expression"),
        ({"cli_home": True, "cli_login": {"args": [], "signed_in": "../x"}}, "inside HOME"),
        ({"cli_home": True, "cli_login": {"args": [], "signed_in": "/etc/x"}}, "String should match pattern"),
        ({"cli_home": True, "cli_login": {"args": [], "home_files": {"a/../b": ""}}}, "inside HOME"),
        ({"cli_home": True, "cli_login": {"args": [], "seconds": 5}}, "greater than or equal to 30"),
        ({"cli_home": True, "cli_login": {}}, "args"),
        ({"cli_allow": [""]}, "at least 1 character"),
        ({"kind": "program", "functions": {"start": {}}}, 'cli is for a manifest of "kind": "cli"'),
    ],
)
def test_refused_cli_manifests(tmp_path, change, why):
    data = cli_manifest()
    for key, value in change.items():
        if key.startswith("cli_"):
            data["cli"][key.removeprefix("cli_")] = value
        elif value is None:
            data.pop(key)
        else:
            data[key] = value
    assert why in refusal(tmp_path, data)


def test_a_program_still_needs_its_program_py(tmp_path):
    (tmp_path / "glyphh.json").write_text(json.dumps({"name": "p", "functions": {"start": {}}}))
    with pytest.raises(DeployError, match=r"no program\.py"):
        m.read(tmp_path)


def test_env_names_the_secrets_it_takes():
    found = m.Manifest.model_validate(
        cli_manifest(cli={"env": {"TOKEN": "secret:TOKEN", "PLAIN": "1"}}, secrets=["TOKEN"])
    )
    assert found.cli is not None and found.cli.secret_refs() == {"TOKEN": "TOKEN"}


def test_a_secret_taken_only_as_secret_question_is_optional():
    login = {"args": ["login"], "env": {"KEY": "secret?:KEY", "BOTH": "secret:BOTH"}}
    env = {"KEY": "secret?:KEY", "TOKEN": "secret:TOKEN", "BOTH": "secret?:BOTH"}
    found = m.Manifest.model_validate(
        cli_manifest(cli={"env": env, "home": True, "login": login}, secrets=["KEY", "TOKEN", "BOTH"])
    )
    assert found.cli is not None and found.cli.optional_secrets() == {"KEY"}
    assert found.cli.secret_refs() == {"KEY": "KEY", "TOKEN": "TOKEN", "BOTH": "BOTH"}
    assert found.cli.secret_refs(login=True)["BOTH"] == "BOTH"
    bare = m.Manifest.model_validate(cli_manifest(cli={"env": {"KEY": "secret?:KEY"}}, secrets=["KEY"]))
    assert bare.cli is not None and bare.cli.optional_secrets() == {"KEY"} and bare.cli.login is None


def test_a_login_block_and_its_defaults():
    found = m.Manifest.model_validate(cli_manifest(cli={"home": True, "login": {"args": ["auth", "login"]}}))
    assert found.cli is not None and found.cli.login is not None
    login = found.cli.login
    assert (login.args, login.listen, login.paste, login.code, login.signed_in, login.seconds) == (
        ["auth", "login"],
        False,
        True,
        None,
        None,
        600,
    )


@pytest.mark.parametrize("example", ["cli_gh", "cli_glyphh"])
def test_the_example_cli_manifests_are_read(example):
    """The examples' manifests only: their versions and checksums are placeholders, nothing is downloaded."""
    found = m.read(EXAMPLES / example)
    assert found.kind == "cli" and found.cli is not None
    assert found.cli.install.sha256 == "0" * 64  # the placeholder, marked as one in the manifest and its README
    assert "PLACEHOLDER" in found.description
    assert set(found.cli.secret_refs().values()) == set(found.secrets)


def test_the_gh_example_installs_from_its_release_and_allows_what_it_says():
    found = m.read(EXAMPLES / "cli_gh")
    assert found.cli is not None and found.cli.install.source == "url" and found.cli.home
    assert "release-assets.githubusercontent.com" in found.cli.install.implied_hosts()
    assert found.cli.approvals and found.cli.allow


def test_the_glyphh_example_is_its_pypi_package():
    found = m.read(EXAMPLES / "cli_glyphh")
    assert found.cli is not None and found.cli.install.pinned.startswith("glyphh==") and found.cli.command == "glyphh"
