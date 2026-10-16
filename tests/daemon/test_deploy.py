"""Deploys through real git: a bundle, a repo over file:// (as GitHub), a commit already held; a real venv."""

from __future__ import annotations

import base64
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from rrotord.deploy import Deployer, Tools, check_sha
from rrotord.errors import BadRequest, DeployError
from rrotord.models import Models
from tests.fakes import GitRepo, manifest, token


def deployer(tmp_path: Path, **tools: object) -> Deployer:
    kw = {"python": sys.executable, "uv": shutil.which("uv") or "uv", "github": f"file://{tmp_path / 'github'}"}
    kw.update(tools)  # type: ignore[arg-type]
    return Deployer(Models(tmp_path / "data"), tmp_path / "scratch", Tools(**kw))  # type: ignore[arg-type]


def test_check_sha():
    assert check_sha("a" * 40) == "a" * 40
    with pytest.raises(BadRequest) as caught:
        check_sha("abc")
    assert caught.value.code == "E_COMMIT"


async def test_from_bundle_installs_and_activates(tmp_path, repo: GitRepo):
    sha = repo.model(manifest("sample"), **{"files/seed.txt": "seed"})
    d = deployer(tmp_path)
    v = await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert (v.model, v.version, v.commit_sha) == ("sample", 1, sha)
    assert (v.src / "model.py").is_file() and not (v.src / ".git").exists()
    assert (v.venv / "bin" / "python").exists()
    assert d.models.active("sample") == v
    assert (d.models.files("sample") / "seed.txt").read_text() == "seed"
    assert list((tmp_path / "scratch").iterdir()) == []


async def test_a_second_version_keeps_the_folder_and_numbers_up(tmp_path, repo: GitRepo):
    d = deployer(tmp_path)
    first = repo.model(manifest("sample"))
    await d.from_bundle(repo.bundle(tmp_path / "1.bundle"), first)
    (d.models.files("sample") / "mine.txt").write_text("kept")
    second = repo.model(manifest("sample"), **{"files/seed.txt": "late"})
    v = await d.from_bundle(repo.bundle(tmp_path / "2.bundle"), second)
    assert v.version == 2
    assert not (d.models.files("sample") / "seed.txt").exists()


async def test_a_model_without_seed_files_gets_an_empty_folder(tmp_path, repo: GitRepo):
    sha = repo.model(manifest("sample"))
    d = deployer(tmp_path)
    await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert list(d.models.files("sample").iterdir()) == []


async def test_deploying_a_commit_already_held_reactivates_it(tmp_path, repo: GitRepo):
    d = deployer(tmp_path)
    first = repo.model(manifest("sample"))
    await d.from_bundle(repo.bundle(tmp_path / "1.bundle"), first)
    second = repo.model(manifest("sample", description="two"))
    await d.from_bundle(repo.bundle(tmp_path / "2.bundle"), second)
    again = await d.from_bundle(repo.bundle(tmp_path / "3.bundle"), first, version=5)
    assert (again.commit_sha, again.version) == (first, 5)
    assert d.models.active("sample") == again


async def test_from_bundle_a_commit_not_in_it(tmp_path, repo: GitRepo):
    repo.model()
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path).from_bundle(repo.bundle(tmp_path / "b.bundle"), "f" * 40)
    assert caught.value.code == "E_GIT" and caught.value.message.startswith("git fetch failed")


async def test_from_bundle_with_a_bad_sha(tmp_path):
    with pytest.raises(BadRequest):
        await deployer(tmp_path).from_bundle(tmp_path / "none", "nope")


async def test_from_bundle_with_a_refused_manifest(tmp_path, repo: GitRepo):
    sha = repo.model({"name": "Bad", "functions": {"start": {}}})
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path).from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert caught.value.code == "E_MANIFEST"


async def test_from_repo_fetches_the_commit_with_its_token(tmp_path, repo: GitRepo):
    sha = repo.model(manifest("sample"))
    repo.bare(tmp_path / "github", "owner/sample")
    v = await deployer(tmp_path).from_repo("owner/sample", sha, token(), version=3)
    assert (v.model, v.version) == ("sample", 3)


async def test_from_repo_without_a_token(tmp_path, repo: GitRepo):
    sha = repo.model(manifest("sample"))
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
    sha = repo.model()
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
    sha = repo.model(manifest("sample"))
    d = deployer(tmp_path, python=str(tmp_path / "no-python"))
    with pytest.raises((DeployError, OSError)):
        await d.from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert d.models.versions("sample") == []
    assert list((tmp_path / "data" / "models" / "sample" / "versions").iterdir()) == []


async def test_a_venv_command_that_fails_is_e_venv(tmp_path, repo: GitRepo):
    sha = repo.model(manifest("sample"))
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
    sha = repo.model(manifest("sample"), **{"requirements.lock": f"tinypkg @ {path.as_uri()} --hash=sha256:{digest}\n"})
    v = await deployer(tmp_path).from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert list(v.venv.glob("lib/python*/site-packages/tinypkg/__init__.py"))


async def test_a_lock_with_a_wrong_hash_is_e_deps(tmp_path, repo: GitRepo):
    path, _ = wheel(tmp_path)
    sha = repo.model(
        manifest("sample"), **{"requirements.lock": f"tinypkg @ {path.as_uri()} --hash=sha256:{'0' * 64}\n"}
    )
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path).from_bundle(repo.bundle(tmp_path / "b.bundle"), sha)
    assert caught.value.code == "E_DEPS"


async def test_held_activates_and_renumbers(tmp_path):
    from tests.fakes import install

    d = deployer(tmp_path)
    v = install(d.models, manifest("sample"), "", sha="c" * 40)
    assert (await d.held("sample", "c" * 40)).version == v.version
    assert (await d.held("sample", "c" * 40, version=v.version)).version == v.version
    assert (await d.held("sample", "c" * 40, version=4)).version == 4


async def test_held_a_commit_not_here(tmp_path):
    with pytest.raises(DeployError) as caught:
        await deployer(tmp_path).held("sample", "c" * 40)
    assert caught.value.code == "E_COMMIT_MISSING"


def test_git_env_carries_extra_config_and_the_token_in_the_environment(tmp_path):
    secret = token()
    env = deployer(tmp_path, git_env={"protocol.file.allow": "always"})._git_env(tmp_path, secret)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("protocol.file.allow", "always")
    basic = base64.b64decode(env["GIT_CONFIG_VALUE_1"].split()[-1]).decode()
    assert basic == f"x-access-token:{secret}"
