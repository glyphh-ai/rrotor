"""Shared fakes and helpers: git repos of sample models, a launcher with no sandbox, and small stand-ins."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import rrotor
from rrotord import manifest as manifests
from rrotord.models import Models, Version
from rrotord.runner.launch import Spec
from rrotord.sandbox import bwrap
from rrotord.sandbox.probe import Layers

GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/tmp",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

DONE_MODEL = """
from rrotor import Result

def start(ctx, inputs):
    return Result.done({"echo": inputs})
"""


def token() -> str:
    """A generated test token, never a real one."""
    return secrets.token_urlsafe(32)


def manifest(name: str = "sample", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"name": name, "functions": {"start": {}}}
    data.update(extra)
    return data


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True)
    return done.stdout.strip()


class GitRepo:
    """A model's repo: write files, commit, bundle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "main")

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit(self, files: dict[str, str] | None = None, message: str = "change") -> str:
        self.write(files or {})
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.path, "rev-parse", "HEAD")

    def model(self, data: dict[str, Any] | None = None, source: str = DONE_MODEL, **files: str) -> str:
        return self.commit({"glyphh.json": json.dumps(data or manifest()), "model.py": source, **files})

    def bundle(self, out: Path) -> Path:
        git(self.path, "bundle", "create", "-q", str(out), "main")
        return out

    def bare(self, root: Path, repo: str) -> None:
        """A bare clone at root/<owner>/<name>.git, reached as `{github}/{repo}.git` over file://."""
        target = root / f"{repo}.git"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.path), str(target)], env=GIT_ENV, check=True, capture_output=True
        )


def install(
    models: Models, data: dict[str, Any], source: str, sha: str = "a" * 40, version: int = 1, files: bool = True
) -> Version:
    """A version on /data as a deploy leaves it, with no git and no venv (the test launcher runs the host Python)."""
    found = manifests.Manifest.model_validate(data)
    root = models.version_root(found.name, sha)
    (root / "src").mkdir(parents=True)
    (root / "src" / "model.py").write_text(source)
    (root / "venv").mkdir()
    if files:
        models.files(found.name).mkdir(parents=True, exist_ok=True)
    made = models.record(found.name, version, sha, found)
    models.activate(made)
    return made


def ready() -> Layers:
    return Layers({"bubblewrap": {"ok": True, "detail": "test"}})


class LocalLauncher:
    """The worker with no sandbox: the host's Python, the version's own folders. Tests only."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.specs: list[Spec] = []

    def sandbox(self, mounts: bwrap.Mounts) -> dict[str, Any] | None:
        return None

    def paths(self, mounts: bwrap.Mounts) -> dict[str, str]:
        return {"model": str(mounts.src), "files": str(mounts.files)}

    async def spawn(self, spec: Spec) -> asyncio.subprocess.Process:
        self.specs.append(spec)
        if self.fail is not None:
            raise self.fail
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(Path(rrotor.__file__).parent.parent),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "RROTOR_CHANNEL_FD": str(spec.channel_fd),
        }
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "rrotor._worker",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(spec.channel_fd,),
            start_new_session=True,
            env=env,
            cwd=str(spec.mounts.src),
        )


class FakeLink:
    """What the handler owes the Cloud, as the link client would keep it."""

    def __init__(self) -> None:
        self.seq = 0
        self.sent: list[dict[str, Any]] = []

    async def owe(self, make: Any, numbered: bool = True) -> None:
        if numbered:
            self.seq += 1
        self.sent.append(make(self.seq))


class FakeProc:
    """A process whose first wait times out (so the caller kills it), then it is gone."""

    def __init__(self, pid: int = 999999) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.waits, self.killed, self.terminated = 0, False, False
        self.stdout = self.stderr = None

    async def wait(self) -> int:
        self.waits += 1
        if self.waits == 1:
            raise TimeoutError
        self.returncode = -9
        return -9

    def kill(self) -> None:
        self.killed = True

    def terminate(self) -> None:
        self.terminated = True
