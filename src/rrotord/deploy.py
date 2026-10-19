"""Deploying a version: exactly one commit, from a git bundle sent to the API, from a GitHub repo (with a token for
a private one; the Cloud mints a short-lived one per deploy), or one this rrotor already holds. The commit's SHA is
checked, the manifest read, a venv made and `requirements.lock` installed (binary wheels only, with hashes: no model
code runs outside the sandbox, not even a package's setup), and the version becomes active (docs/architecture.md,
2.2 and 5)."""

from __future__ import annotations

import base64
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rrotor import ada
from rrotor.errors import ModelError
from rrotord import manifest as manifests
from rrotord.errors import BadRequest, DeployError
from rrotord.models import Models, Version
from rrotord.proc import run

SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


@dataclass(frozen=True, slots=True)
class Tools:
    git: str = "git"
    uv: str = "uv"
    python: str = "/usr/bin/python3.13"
    github: str = "https://github.com"
    git_env: Mapping[str, str] | None = None  # extra git config, for tests (file:// transport)


def check_sha(commit: str) -> str:
    if not SHA.match(commit):
        raise BadRequest("A commit is its full 40-character SHA.", code="E_COMMIT")
    return commit


Maintain = Callable[[Version, str], Awaitable[dict[str, Any]]]


class Deployer:
    def __init__(
        self, models: Models, scratch: Path, tools: Tools | None = None, maintain: Maintain | None = None
    ) -> None:
        self.models, self.scratch, self.tools = models, scratch, tools or Tools()
        self.maintain = maintain  # runs rrotor's own operation in a version's sandbox (the runner's)

    @staticmethod
    def ada_spec(src: Path, found: manifests.Manifest) -> dict[str, Any] | None:
        """An Ada model's spec (it names the part `ada`): read and checked, with its data folder required."""
        if "ada" not in found.needs:
            return None
        if not found.data:
            raise DeployError(
                'An Ada model keeps its precedents in its data folder: set "data": true.', code="E_MANIFEST"
            )
        try:
            return ada.read_spec(src)
        except ModelError as err:
            raise DeployError(err.message, code="E_MANIFEST") from None

    async def _ada_ready(self, made: Version, spec: dict[str, Any] | None) -> None:
        """Before an Ada version goes active: a changed encoding re-encodes the store, or the deploy is refused."""
        if spec is None:
            return
        before = self.models.active(made.model)
        store = self.models.data(made.model) / ada.STORE
        if before is None or before.commit_sha == made.commit_sha or not store.is_file():
            return
        try:
            old = ada.read_spec(before.src)
        except ModelError:
            return
        if ada.encoding_of(old) == ada.encoding_of(spec):
            return
        if self.maintain is None:
            raise DeployError(
                "The spec's encoding changed and the store cannot be re-encoded here.", code="E_ADA_ENCODING"
            )
        result = await self.maintain(made, ada.REENCODE)
        if result.get("status") != "done":
            why = (result.get("data") or {}).get("reason", "the re-encode did not finish")
            raise DeployError(
                f"The spec's encoding changed and the store could not be re-encoded: {why}", code="E_ADA_ENCODING"
            )

    def _git_env(self, home: Path, token: str | None = None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        pairs = list((self.tools.git_env or {}).items())
        if token:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            pairs.append(("http.extraHeader", f"Authorization: Basic {basic}"))  # in the environment, never an argument
        env["GIT_CONFIG_COUNT"] = str(len(pairs))
        for i, (key, value) in enumerate(pairs):
            env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
        return env

    async def _git(self, env: dict[str, str], *args: str, cwd: Path | None = None) -> str:
        done = await run([self.tools.git, *args], env=env, cwd=cwd, timeout=300)
        if done.code != 0:
            raise DeployError(f"git {args[0]} failed: {done.why()}", code="E_GIT")
        return done.stdout.strip()

    async def from_bundle(self, bundle: Path, commit: str, version: int | None = None) -> Version:
        check_sha(commit)
        work = Path(tempfile.mkdtemp(dir=self._scratch()))
        try:
            env = self._git_env(work)
            src = work / "src"
            await self._git(env, "init", "-q", str(src))
            await self._git(env, "fetch", "-q", str(bundle), commit, cwd=src)
            return await self._install(env, src, commit, version)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def from_repo(self, repo: str, commit: str, token: str | None, version: int | None = None) -> Version:
        check_sha(commit)
        if not REPO.match(repo):
            raise BadRequest("A repo is owner/name on GitHub.", code="E_REPO")
        work = Path(tempfile.mkdtemp(dir=self._scratch()))
        try:
            env = self._git_env(work, token)
            src = work / "src"
            await self._git(env, "init", "-q", str(src))
            await self._git(env, "fetch", "-q", "--depth", "1", f"{self.tools.github}/{repo}.git", commit, cwd=src)
            return await self._install(env, src, commit, version)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def held(self, model: str, commit: str, version: int | None = None) -> Version:
        """A commit this rrotor already holds (a deploy without a repo): made active, renumbered to the Cloud's."""
        found = self.models.by_sha(model, check_sha(commit))
        if found is None:
            raise DeployError(
                f"This rrotor does not hold {model} at {commit[:12]}; deploy it from its folder first.",
                code="E_COMMIT_MISSING",
            )
        if version is not None and version != found.version:
            found = self.models.renumber(found, version)
        await self._ada_ready(found, self.ada_spec(found.src, found.manifest))
        self.models.activate(found)
        return found

    def _scratch(self) -> Path:
        self.scratch.mkdir(parents=True, exist_ok=True)
        return self.scratch

    async def _install(self, env: dict[str, str], src: Path, commit: str, version: int | None) -> Version:
        await self._git(env, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD", cwd=src)
        head = await self._git(env, "rev-parse", "HEAD", cwd=src)
        if head != commit:
            raise DeployError(f"The commit fetched is {head[:12]}, not {commit[:12]}.", code="E_SHA")
        found = manifests.read(src)
        spec = self.ada_spec(src, found)
        shutil.rmtree(src / ".git")
        existing = self.models.by_sha(found.name, commit)
        if existing is not None:
            return await self.held(found.name, commit, version)
        root = self.models.version_root(found.name, commit)
        staging = root.with_name(f".{commit}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            shutil.move(src, staging / "src")
            await self._venv(staging)
            shutil.rmtree(root, ignore_errors=True)
            staging.rename(root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._seed(found.name, root / "src" / "files")
        made = self.models.record(found.name, version or self.models.next_number(found.name), commit, found)
        try:
            await self._ada_ready(made, spec)
        except DeployError:
            self.models.remove(made)
            raise
        self.models.activate(made)
        return made

    async def _venv(self, root: Path) -> None:
        venv = root / "venv"
        done = await run([self.tools.python, "-m", "venv", "--without-pip", str(venv)], timeout=120)
        if done.code != 0:
            raise DeployError(f"The venv could not be made: {done.why()}", code="E_VENV")
        lock = root / "src" / "requirements.lock"
        if not lock.is_file():
            return
        argv = [
            self.tools.uv,
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-deps",
            "-r",
            str(lock),
        ]
        done = await run(
            argv, env={"PATH": os.environ.get("PATH", ""), "HOME": str(root), "UV_NO_CACHE": "1"}, timeout=600
        )
        if done.code != 0:
            raise DeployError(f"requirements.lock could not be installed: {done.why()}", code="E_DEPS")

    def _seed(self, name: str, seed: Path) -> None:
        """A version's files/ seed its folder on the model's first deploy; never over what is there."""
        folder = self.models.files(name)
        if folder.exists():
            return
        if seed.is_dir():
            shutil.copytree(seed, folder)
        else:
            folder.mkdir(parents=True)
