"""Deploying a version: exactly one commit, from a git bundle sent to the API, from a GitHub repo (with a token for
a private one; the Cloud mints a short-lived one per deploy), or one this rrotor already holds. The commit's SHA is
checked, the manifest read, a venv made and `requirements.lock` installed (binary wheels only, with hashes: no program
code runs outside the sandbox, not even a package's setup), and the version becomes active (docs/architecture.md,
2.2 and 5). A CLI's version also gets its install, pinned and checked, under `cli/` (rrotord/clis.py)."""

from __future__ import annotations

import base64
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rrotord import clis
from rrotord import manifest as manifests
from rrotord.errors import BadRequest, DeployError
from rrotord.proc import run
from rrotord.programs import Programs, Version

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


class Deployer:
    def __init__(
        self, programs: Programs, scratch: Path, tools: Tools | None = None, fetch: clis.Fetch | None = None
    ) -> None:
        self.programs, self.scratch, self.tools = programs, scratch, tools or Tools()
        self.installer = clis.Installer(fetch, uv=self.tools.uv, python=self.tools.python)

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

    async def held(self, program: str, commit: str, version: int | None = None) -> Version:
        """A commit this rrotor already holds (a deploy without a repo): made active, renumbered to the Cloud's."""
        found = self.programs.by_sha(program, check_sha(commit))
        if found is None:
            raise DeployError(
                f"This rrotor does not hold {program} at {commit[:12]}; deploy it from its folder first.",
                code="E_COMMIT_MISSING",
            )
        if version is not None and version != found.version:
            found = self.programs.renumber(found, version)
        self.programs.activate(found)
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
        shutil.rmtree(src / ".git")
        existing = self.programs.by_sha(found.name, commit)
        if existing is not None:
            return await self.held(found.name, commit, version)
        root = self.programs.version_root(found.name, commit)
        staging = root.with_name(f".{commit}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            shutil.move(src, staging / "src")
            await self._venv(staging, lock=found.kind == "program")
            if found.kind == "cli":  # its install, pinned and checked, beside the venv its worker runs in
                await self.installer.install(found, staging / "cli", staging / "src")
            shutil.rmtree(root, ignore_errors=True)
            staging.rename(root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._seed(found.name, root / "src" / "files")
        made = self.programs.record(found.name, version or self.programs.next_number(found.name), commit, found)
        self.programs.activate(made)
        return made

    async def _venv(self, root: Path, lock: bool = True) -> None:
        """The version's venv, and its requirements.lock in it (a CLI's lock is its install's, not the venv's)."""
        venv = root / "venv"
        done = await run([self.tools.python, "-m", "venv", "--without-pip", str(venv)], timeout=120)
        if done.code != 0:
            raise DeployError(f"The venv could not be made: {done.why()}", code="E_VENV")
        locked = root / "src" / "requirements.lock"
        if not lock or not locked.is_file():
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
            str(locked),
        ]
        done = await run(
            argv, env={"PATH": os.environ.get("PATH", ""), "HOME": str(root), "UV_NO_CACHE": "1"}, timeout=600
        )
        if done.code != 0:
            raise DeployError(f"requirements.lock could not be installed: {done.why()}", code="E_DEPS")

    def _seed(self, name: str, seed: Path) -> None:
        """A version's files/ seed its folder on the program's first deploy; never over what is there."""
        folder = self.programs.files(name)
        if folder.exists():
            return
        if seed.is_dir():
            shutil.copytree(seed, folder)
        else:
            folder.mkdir(parents=True)
