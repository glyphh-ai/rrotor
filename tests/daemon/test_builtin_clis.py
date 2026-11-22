"""The vendors' agent CLIs the image carries: their pins (image/clis.json) and the build script that installs them
(image/clis.py, nothing downloaded here), deployed at boot as built-in CLIs, never over one the person deployed."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

import rrotor
from rrotord import manifest as m
from rrotord.config import Config
from rrotord.daemon import Daemon
from rrotord.deploy import Deployer, Tools
from rrotord.errors import DeployError
from rrotord.programs import Programs
from tests.fakes import LocalLauncher, cli_manifest, install, manifest, ready, sha, tarball

IMAGE = Path(__file__).resolve().parents[2] / "image"
PINS = json.loads((IMAGE / "clis.json").read_text())["clis"]
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("image_clis", IMAGE / "clis.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


script = _script()


@pytest.mark.parametrize("name", sorted(PINS))
def test_each_pinned_cli_has_both_builds_and_a_manifest_rrotor_takes(name):
    entry = PINS[name]
    if "sdk" in entry:  # a console script of an SDK image/sdk.lock pins at the same version
        assert f"{name}=={entry['version']} --hash=sha256:" in (IMAGE / "sdk.lock").read_text()
    else:
        assert set(entry["builds"]) == {"amd64", "arm64"}
    for build in entry.get("builds", {}).values():
        assert build["url"].startswith("https://") and HEX64.match(build["sha256"]) and build["entry"]
        assert entry["version"] in build["url"]
    found = m.Manifest.model_validate(script.manifest(name, entry))
    assert found.kind == "cli" and found.cli is not None and found.cli.home and found.cli.login is not None
    assert found.cli.install.image == f"/usr/local/bin/{entry['command']}" and found.cli.command == entry["command"]
    assert found.hosts and (found.cli.allow or "sdk" in entry)
    assert found.cli.optional_secrets() == set(found.secrets)  # an API key is optional: the sign-in is the way in
    assert script.about(entry) == {"vendor": entry["vendor"], "title": entry["title"], "version": entry["version"]}


def test_the_vendors_the_image_carries():
    assert sorted(PINS) == ["claude", "codex", "copilot", "cursor", "gemini", "glyphh", "grok"]
    assert PINS["claude"]["manifest"]["cli"]["login"]["args"] == ["auth", "login", "--claudeai"]  # claude.ai


def test_the_build_script_checks_unpacks_links_and_writes_the_manifest(tmp_path):
    archive = tarball({"package/bin/tool": "#!/bin/sh\necho hi\n"})
    entry = {
        **PINS["claude"],
        "command": "tool",
        "builds": {"arm64": {"url": "https://example.com/t.tgz", "sha256": sha(archive), "entry": "package/bin/tool"}},
    }
    link = script.install("tool", entry, tmp_path, "arm64", fetch=lambda url: archive)
    assert link == tmp_path / "usr/local/bin/tool"
    assert link.readlink() == Path("/usr/local/lib/rrotor-clis/tool/package/bin/tool")
    folder = tmp_path / "usr/local/lib/rrotor-clis/tool"
    assert (folder / "package/bin/tool").stat().st_mode & 0o111
    written = json.loads((folder / "glyphh.json").read_text())
    assert written["cli"]["install"] == {"image": "/usr/local/bin/tool"} and written["name"] == "tool"
    assert json.loads((folder / "vendor.json").read_text())["vendor"] == "Anthropic"


def test_the_build_script_refuses_a_wrong_checksum_or_a_missing_entry(tmp_path):
    archive = tarball({"package/x": "x"})
    build = {"url": "https://example.com/t.tgz", "sha256": "0" * 64, "entry": "package/x"}
    entry = {**PINS["claude"], "builds": {"amd64": build}}
    with pytest.raises(SystemExit, match="not the pinned"):
        script.install("claude", entry, tmp_path / "a", "amd64", fetch=lambda url: archive)
    entry["builds"]["amd64"] = {**build, "sha256": sha(archive), "entry": "package/missing"}
    with pytest.raises(SystemExit, match="not a file in its package"):
        script.install("claude", entry, tmp_path / "b", "amd64", fetch=lambda url: archive)
    with pytest.raises(SystemExit, match="arch is one of"):
        script.main(["clis.py", str(IMAGE / "clis.json"), str(tmp_path), "riscv"])
    with pytest.raises(SystemExit, match="is not https"):
        script.download("http://example.com/x")


def test_the_build_script_writes_an_sdk_clis_console_script(tmp_path):
    with pytest.raises(SystemExit, match=r"install image/sdk\.lock first"):
        script.install("glyphh", PINS["glyphh"], tmp_path, "arm64")
    (tmp_path / "usr/local/lib/rrotor-sdk/glyphh").mkdir(parents=True)
    link = script.install("glyphh", PINS["glyphh"], tmp_path, "arm64")
    assert link.readlink() == Path("/usr/local/lib/rrotor-clis/glyphh/glyphh")
    wrapper = (tmp_path / "usr/local/lib/rrotor-clis/glyphh/glyphh").read_text()
    assert wrapper.startswith("#!/usr/bin/python3\n") and "from glyphh.cli import main" in wrapper
    assert '"/usr/local/lib/rrotor-sdk"' in wrapper
    assert json.loads((tmp_path / "usr/local/lib/rrotor-clis/glyphh/vendor.json").read_text())["version"] == "3.12.0"


@pytest.mark.skipif(shutil.which("node") is None, reason="node unpacks a brotli-packed binary")
def test_the_build_script_unpacks_a_brotli_packed_binary(tmp_path):
    import subprocess

    raw = tmp_path / "raw"
    raw.write_text("#!/bin/sh\necho grok\n")
    js = (
        "const z=require('zlib'),f=require('fs');"
        "f.writeFileSync(process.argv[2],z.brotliCompressSync(f.readFileSync(process.argv[1])))"
    )
    subprocess.run(["node", "-e", js, str(raw), str(tmp_path / "packed")], check=True)
    archive = tarball({"package/bin/grok.br": (tmp_path / "packed").read_bytes()})
    build = {"url": "https://x.example/g.tgz", "sha256": sha(archive), "entry": "package/bin/grok"}
    entry = {**PINS["grok"], "builds": {"arm64": {**build, "brotli": "package/bin/grok.br"}}}
    script.install("grok", entry, tmp_path / "root", "arm64", fetch=lambda url: archive)
    folder = tmp_path / "root/usr/local/lib/rrotor-clis/grok/package/bin"
    assert (folder / "grok").read_text() == raw.read_text() and not (folder / "grok.br").exists()


# Deployed at boot


def builtin_folder(root: Path, name: str = "shell", version: str = "1.0", **cli: Any) -> Path:
    """A built-in CLI as the image carries it: its manifest installing the image's sh, its vendor file."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    block = {"install": {"image": "sh"}, "command": "sh", "home": True, "login": {"args": ["login"]}} | cli
    (folder / "glyphh.json").write_text(json.dumps(cli_manifest(name, cli=block)))
    (folder / "vendor.json").write_text(json.dumps({"vendor": "Acme", "title": "Shell", "version": version}))
    return folder


def deployer(tmp_path: Path) -> Deployer:
    tools = Tools(python=sys.executable, uv=shutil.which("uv") or "uv")
    return Deployer(Programs(tmp_path / "data"), tmp_path / "scratch", tools)


async def test_a_builtin_cli_is_deployed_once_and_again_when_its_pins_change(tmp_path):
    d = deployer(tmp_path)
    folder = builtin_folder(tmp_path / "image")
    first = await d.builtin(folder)
    assert first is not None and first.builtin == {"vendor": "Acme", "title": "Shell", "version": "1.0"}
    assert (first.version, d.programs.active("shell")) == (1, first)
    assert await d.builtin(folder) is None  # the same pins: nothing to do
    listed = d.programs.clis()[0]
    assert listed["builtin"]["version"] == "1.0" and listed["login"] == {"args": ["login"]}
    assert d.programs.deployed() == []  # the image's own, not the Cloud's to know about
    newer = await d.builtin(builtin_folder(tmp_path / "image", version="2.0"))
    assert newer is not None and newer.version == 2 and d.programs.active("shell") == newer
    again = await d.builtin(builtin_folder(tmp_path / "image", version="1.0"))  # the image rolled back
    assert again is not None and again.commit_sha == first.commit_sha and d.programs.active("shell") == again


async def test_a_cli_the_person_deployed_is_never_replaced(tmp_path):
    d = deployer(tmp_path)
    install(d.programs, cli_manifest("shell"), "", sha="e" * 40)
    assert await d.builtin(builtin_folder(tmp_path / "image")) is None
    assert d.programs.active("shell").builtin is None  # type: ignore[union-attr]
    install(d.programs, manifest("other"), "def start(ctx, inputs):\n    pass\n", sha="f" * 40)
    (d.programs.folder("other") / "active.json").unlink()  # no active version: still theirs
    assert await d.builtin(builtin_folder(tmp_path / "image", "other")) is None


async def test_a_builtin_whose_install_fails_leaves_nothing(tmp_path):
    d = deployer(tmp_path)
    folder = builtin_folder(tmp_path / "image", install={"image": "no-such-binary"}, command="no-such-binary")
    with pytest.raises(DeployError, match="The image has no no-such-binary"):
        await d.builtin(folder)
    assert d.programs.names() == [] and not (d.programs.root / "shell").exists()
    await d.builtin(builtin_folder(tmp_path / "image"))
    with pytest.raises(DeployError):  # a new version that fails: the one there is kept
        await d.builtin(builtin_folder(tmp_path / "image", version="2", install={"image": "nope"}, command="nope"))
    assert d.programs.active("shell").builtin["version"] == "1.0"  # type: ignore[union-attr,index]


async def test_the_daemon_deploys_the_image_clis_at_boot_and_logs_a_bad_one(tmp_path, short_dir, caplog):
    image = tmp_path / "image"
    builtin_folder(image)
    builtin_folder(image, "broken", install={"image": "no-such-binary"}, command="no-such-binary")
    c = Config(
        tmp_path / "data",
        "local",
        "http://127.0.0.1:1",
        0,
        0,
        None,
        Path(rrotor.__file__).parent,
        short_dir,
        builtin_clis=image,
    )
    d = daemon(c)
    with caplog.at_level("WARNING", logger="rrotor"):
        assert await d.install_builtin_clis() == ["shell v1"]
    assert "a built-in CLI was not deployed" in caplog.text
    assert await d.install_builtin_clis() == []
    assert await daemon(dataclasses.replace(c, builtin_clis=tmp_path / "nowhere")).install_builtin_clis() == []
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level("INFO", logger="rrotor"):
        await daemon(dataclasses.replace(c, data=tmp_path / "data2")).serve(stop)
    assert "built-in CLI deployed" in caplog.text


def daemon(c: Config) -> Daemon:
    """A daemon whose deploys make their venv with this Python (the image's is /usr/bin/python3.13)."""
    d = Daemon(c, ready(), launcher=LocalLauncher())
    d.deployer = Deployer(d.runner.programs, c.run_dir / "deploy", Tools(python=sys.executable))
    return d
