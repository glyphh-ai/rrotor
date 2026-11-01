"""Deploys through real git: a bundle, a repo over file:// (as GitHub), a commit already held; a real venv."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from rrotord.deploy import Deployer, Tools, check_sha
from rrotord.errors import BadRequest, DeployError
from rrotord.programs import Programs
from tests.fakes import GitRepo, manifest, token


def deployer(tmp_path: Path, **tools: object) -> Deployer:
    kw = {"python": sys.executable, "uv": shutil.which("uv") or "uv", "github": f"file://{tmp_path / 'github'}"}
    kw.update(tools)  # type: ignore[arg-type]
    return Deployer(Programs(tmp_path / "data"), tmp_path / "scratch", Tools(**kw))  # type: ignore[arg-type]


def test_check_sha():
    assert check_sha("a" * 40) == "a" * 40
    with pytest.raises(BadRequest) as caught:
        check_sha("abc")
    assert caught.value.code == "E_COMMIT"


async def test_from_bundle_installs_and_activates(tmp_path, repo: GitRepo):
    sha = repo.program(manifest("sample"), **{"files/seed.txt": "seed"})
    d = deployer(tmp_path)
    v = await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert (v.program, v.version, v.commit_sha) == ("sample", 1, sha)
    assert (v.src / "program.py").is_file() and not (v.src / ".git").exists()
    assert (v.venv / "bin" / "python").exists()
    assert d.programs.active("sample") == v
    assert (d.programs.files("sample") / "seed.txt").read_text() == "seed"
    assert list((tmp_path / "scratch").iterdir()) == []


async def test_a_program_naming_a_cli_not_here_is_refused(tmp_path, repo: GitRepo):
    from tests.fakes import cli_manifest, install

    d = deployer(tmp_path)
    install(d.programs, manifest("plain"), "def start(ctx, inputs):\n    pass\n", sha="e" * 40)
    install(d.programs, cli_manifest("idle"), "", sha="d" * 40)
    (d.programs.folder("idle") / "active.json").unlink()
    sha = repo.program(manifest("sample", clis=["claude", "plain", "idle"]))
    with pytest.raises(DeployError, match="names the CLIs claude, plain, idle, not on this rrotor") as caught:
        await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert caught.value.code == "E_MANIFEST"
    install(d.programs, cli_manifest("claude"), "", sha="f" * 40)
    sha = repo.program(manifest("sample", clis=["claude"]))
    v = await d.from_bundle(repo.bundle(tmp_path / "c.bundle"), sha)
    assert v.manifest.clis == ["claude"]


async def test_a_second_version_keeps_the_folder_and_numbers_up(tmp_path, repo: GitRepo):
    d = deployer(tmp_path)
    first = repo.program(manifest("sample"))
    await d.from_bundle(repo.bundle(tmp_path / "1.bundle"), first)
    (d.programs.files("sample") / "mine.txt").write_text("kept")
    second = repo.program(manifest("sample"), **{"files/seed.txt": "late"})
    v = await d.from_bundle(repo.bundle(tmp_path / "2.bundle"), second)
    assert v.version == 2
    assert not (d.programs.files("sample") / "seed.txt").exists()


async def test_a_program_without_seed_files_gets_an_empty_folder(tmp_path, repo: GitRepo):
    sha = repo.program(manifest("sample"))
    d = deployer(tmp_path)
    await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert list(d.programs.files("sample").iterdir()) == []


async def test_deploying_a_commit_already_held_reactivates_it(tmp_path, repo: GitRepo):
    d = deployer(tmp_path)
    first = repo.program(manifest("sample"))
    await d.from_bundle(repo.bundle(tmp_path / "1.bundle"), first)
    second = repo.program(manifest("sample", description="two"))
    await d.from_bundle(repo.bundle(tmp_path / "2.bundle"), second)
    again = await d.from_bundle(repo.bundle(tmp_path / "3.bundle"), first, version=5)
    assert (again.commit_sha, again.version) == (first, 5)
    assert d.programs.active("sample") == again


async def test_from_bundle_a_commit_not_in_it(tmp_path, repo: GitRepo):
    repo.program()
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path).from_bundle(repo.bundle(tmp_path / "b.bundle"), "f" * 40)
    assert caught.value.code == "E_GIT" and caught.value.message.startswith("git fetch failed")


async def test_from_bundle_with_a_bad_sha(tmp_path):
    with pytest.raises(BadRequest):
        await deployer(tmp_path).from_bundle(tmp_path / "none", "nope")


async def test_from_bundle_with_a_refused_manifest(tmp_path, repo: GitRepo):
    sha = repo.program({"name": "Bad", "functions": {"start": {}}})
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path).from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert caught.value.code == "E_MANIFEST"


async def test_from_repo_fetches_the_commit_with_its_token(tmp_path, repo: GitRepo):
    sha = repo.program(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    v = await deployer(tmp_path).from_repo("owner/sample", sha, token(), version=3)
    assert (v.program, v.version) == ("sample", 3)


async def test_from_repo_without_a_token(tmp_path, repo: GitRepo):
    sha = repo.program(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    assert (await deployer(tmp_path).from_repo("owner/sample", sha, None)).version == 1


async def test_from_repo_refuses_a_repo_that_is_not_owner_name(tmp_path):
    with pytest.raises(BadRequest) as caught:
        await deployer(tmp_path).from_repo("not a repo", "a" * 40, None)
    assert caught.value.code == "E_REPO"


async def test_from_repo_that_is_not_there(tmp_path):
    with pytest.raises(DeployError, match="git fetch failed"):
        await deployer(tmp_path).from_repo("owner/missing", "a" * 40, None)


async def test_a_fetched_head_that_is_not_the_commit_is_refused(tmp_path, repo: GitRepo, monkeypatch):
    sha = repo.program()
    d = deployer(tmp_path)
    real = d._git

    async def lying(env, *args, cwd=None):
        out = await real(env, *args, cwd=cwd)
        return "0" * 40 if args[0] == "rev-parse" else out

    monkeypatch.setattr(d, "_git", lying)
    with pytest.raises(DeployError) as caught:
        await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert caught.value.code == "E_SHA"


async def test_a_venv_that_cannot_be_made_leaves_no_version(tmp_path, repo: GitRepo):
    sha = repo.program(manifest("sample"))
    d = deployer(tmp_path, python=str(tmp_path / "no-python"))
    with pytest.raises((DeployError, OSError)):
        await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert d.programs.versions("sample") == []
    assert list((tmp_path / "data" / "programs" / "sample" / "versions").iterdir()) == []


async def test_a_venv_command_that_fails_is_e_venv(tmp_path, repo: GitRepo):
    sha = repo.program(manifest("sample"))
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path, python="false").from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert caught.value.code == "E_VENV"


def wheel(folder: Path) -> tuple[Path, str]:
    """A tiny binary wheel, made here: no network, no setup code."""
    path = folder / "tinypkg-0.1-py3-none-any.whl"
    files = {
        "tinypkg/__init__.py": "VALUE = 42\n",
        "tinypkg-0.1.dist-info/METADATA": "Metadata-Version: 2.1\nName: tinypkg\nVersion: 0.1\n",
        "tinypkg-0.1.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = []
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
            digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
            record.append(f"{name},sha256={digest},{len(text)}")
        record.append("tinypkg-0.1.dist-info/RECORD,,")
        z.writestr("tinypkg-0.1.dist-info/RECORD", "\n".join(record) + "\n")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


async def test_requirements_lock_is_installed_into_the_venv(tmp_path, repo: GitRepo):
    path, digest = wheel(tmp_path)
    sha = repo.program(
        manifest("sample"), **{"requirements.lock": f"tinypkg @ {path.as_uri()} --hash=sha256:{digest}\n"}
    )
    v = await deployer(tmp_path).from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert list(v.venv.glob("lib/python*/site-packages/tinypkg/__init__.py"))


async def test_a_lock_with_a_wrong_hash_is_e_deps(tmp_path, repo: GitRepo):
    path, _ = wheel(tmp_path)
    sha = repo.program(
        manifest("sample"), **{"requirements.lock": f"tinypkg @ {path.as_uri()} --hash=sha256:{'0' * 64}\n"}
    )
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path).from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert caught.value.code == "E_DEPS"


async def test_held_activates_and_renumbers(tmp_path):
    from tests.fakes import install

    d = deployer(tmp_path)
    v = install(d.programs, manifest("sample"), "", sha="c" * 40)
    assert (await d.held("sample", "c" * 40)).version == v.version
    assert (await d.held("sample", "c" * 40, version=v.version)).version == v.version
    assert (await d.held("sample", "c" * 40, version=4)).version == 4


async def test_held_a_commit_not_here(tmp_path):
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path).held("sample", "c" * 40)
    assert caught.value.code == "E_COMMIT_MISSING"


async def test_a_cli_deploys_with_its_install_and_no_program_py(tmp_path, repo: GitRepo):
    from tests.fakes import ECHO, FakeFetch, cli_manifest, console_wheel, sha, tarball

    archive = tarball({"bin/tool": ECHO})
    url = "https://example.com/tool.tar.gz"
    d = deployer(tmp_path)
    d.installer.fetch = FakeFetch({url: archive})
    block = {"install": {"url": url, "sha256": sha(archive)}}
    commit = repo.commit({"glyphh.json": json.dumps(cli_manifest(cli=block))})
    v = await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), commit)
    assert v.manifest.kind == "cli" and (v.root / "cli" / "installed.json").is_file()
    assert (v.venv / "bin" / "python").exists() and not (v.src / "program.py").exists()
    path, digest = wheel(tmp_path)  # a CLI's lock is its install's: never the venv's
    wheel_bytes = console_wheel()
    pypi = {"install": {"pypi": "hello==1.0", "sha256": sha(wheel_bytes)}, "command": "hello"}
    whl = "https://files.pythonhosted.org/hello-1.0-py3-none-any.whl"
    listing = {"urls": [{"packagetype": "bdist_wheel", "digests": {"sha256": sha(wheel_bytes)}, "url": whl}]}
    answers = {"https://pypi.org/pypi/hello/1.0/json": json.dumps(listing).encode(), whl: wheel_bytes}
    d.installer.fetch = FakeFetch(answers)
    lock = f"tinypkg @ {path.as_uri()} --hash=sha256:{digest}\n"
    second = repo.commit({"glyphh.json": json.dumps(cli_manifest(cli=pypi)), "requirements.lock": lock})
    v2 = await d.from_bundle(repo.bundle(tmp_path / "c.bundle"), second)
    assert (v2.root / "cli" / "site" / "tinypkg").is_dir()
    assert not list(v2.venv.glob("lib/python*/site-packages/tinypkg"))


async def test_a_cli_whose_install_fails_leaves_nothing(tmp_path, repo: GitRepo):
    from tests.fakes import FakeFetch, cli_manifest

    d = deployer(tmp_path)
    d.installer.fetch = FakeFetch()
    commit = repo.commit({"glyphh.json": json.dumps(cli_manifest())})
    with pytest.raises(DeployError) as caught:
        await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), commit)
    assert caught.value.code == "E_CLI_INSTALL" and d.programs.versions("tool") == []


APP = {"name": "board", "kind": "app", "app": {"title": "Board"}}


async def test_from_files_commits_the_page_and_deploys_it_as_an_app(tmp_path):
    d = deployer(tmp_path)
    files = {"glyphh.json": json.dumps(APP).encode(), "index.html": b"<p>one</p>", "css/a.css": b"p{}"}
    v, made = await d.from_files(files)
    assert made and (v.program, v.version, v.manifest.kind) == ("board", 1, "app")
    assert (v.src / "css" / "a.css").read_bytes() == b"p{}" and not (v.src / ".git").exists()
    assert d.programs.active("board") == v and (v.venv / "bin" / "python").exists()
    again, made = await d.from_files(files)  # the same files: the same commit, held already
    assert not made and again.commit_sha == v.commit_sha and d.programs.versions("board") == [v]
    files["index.html"] = b"<p>two</p>"
    two, made = await d.from_files(files, activate=False)
    assert made and two.version == 2 and d.programs.active("board") == v  # deployed, not active
    assert list((tmp_path / "scratch").iterdir()) == []


async def test_from_files_with_a_refused_manifest_leaves_nothing(tmp_path):
    d = deployer(tmp_path)
    with pytest.raises(DeployError) as caught:
        await d.from_files({"glyphh.json": b'{"name": "board", "kind": "app"}', "index.html": b"x"})
    assert caught.value.code == "E_MANIFEST" and d.programs.names() == []


def test_git_env_carries_extra_config_and_the_token_in_the_environment(tmp_path):
    secret = token()
    env = deployer(tmp_path, git_env={"protocol.file.allow": "always"})._git_env(tmp_path, secret)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("protocol.file.allow", "always")
    basic = base64.b64decode(env["GIT_CONFIG_VALUE_1"].split()[-1]).decode()
    assert basic == f"x-access-token:{secret}"
