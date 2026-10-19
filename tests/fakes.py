"""Shared fakes and helpers: git repos of sample models, a launcher with no sandbox, and small stand-ins."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import rrotor
from rrotord import manifest as manifests
from rrotord.keys import canonical
from rrotord.models import Models, Version
from rrotord.runner.launch import Spec
from rrotord.sandbox import bwrap
from rrotord.sandbox.probe import Layers


def _load_part_script() -> Any:
    spec = importlib.util.spec_from_file_location("part_script", Path(__file__).parent.parent / "scripts" / "part.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


part = _load_part_script()

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


@dataclass(frozen=True)
class Keys:
    """A signing key pair made for the tests: the private half signs parts and licence keys, the public one is what a
    rrotor under test is configured with (the licence key's: tests/conftest.py patches it in)."""

    private: Ed25519PrivateKey
    public: Ed25519PublicKey

    @property
    def public_text(self) -> str:
        return base64.b64encode(self.public.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    @property
    def public_url(self) -> str:
        """The public half as rrotord/licence_key.py carries one: base64url of the raw 32 bytes, unpadded."""
        return b64url(self.public.public_bytes(Encoding.Raw, PublicFormat.Raw))

    def key(self, drop: tuple[str, ...] = (), **fields: Any) -> str:
        """A licence key, `<payload>.<signature>`, signed by this pair: a Pro key, live until 2999, unless `fields`
        say otherwise (`drop` leaves fields out)."""
        payload: dict[str, Any] = {
            "license_id": str(uuid.uuid4()),
            "tier": "pro",
            "max_nodes": 3,
            "max_precedents": 50000,
            "cluster_sync_enabled": True,
            "issued_at": "2026-10-01T00:00:00Z",
            "expires_at": "2999-01-01T00:00:00Z",
            "customer_id": "cus_1",
        } | fields
        raw = json.dumps({k: v for k, v in payload.items() if k not in drop}).encode()
        return f"{b64url(raw)}.{b64url(self.private.sign(raw))}"

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self.private.sign(message)).decode()

    def licence(
        self, mods: list[str], expires: str = "2999-01-01T00:00:00Z", plan: str = "Pro", **fields: Any
    ) -> dict[str, Any]:
        payload = {"plan": plan, "mods": {m: {"limits": {}} for m in mods}, "expires_at": expires, "install_id": "i-1"}
        payload |= fields
        return {"payload": payload, "signature": self.sign(canonical(payload))}

    def token(self, signed: dict[str, Any]) -> str:
        """An offline licence as the platform hands it out: base64url of {payload, signature}, unpadded."""
        return base64.urlsafe_b64encode(json.dumps(signed).encode()).decode().rstrip("=")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


_KEYS: Keys | None = None


def keys() -> Keys:
    """One pair for the whole test run."""
    global _KEYS
    if _KEYS is None:
        private = Ed25519PrivateKey.generate()
        _KEYS = Keys(private, private.public_key())
    return _KEYS


def part_archive(
    tmp: Path,
    name: str = "ada",
    version: str = "1.0.0",
    mod: str = "ada",
    arch: str = "arm64",
    files: dict[str, str | bytes] | None = None,
    **info: Any,
) -> tuple[bytes, str, str]:
    """A part's tarball as the build script makes it, with its SHA-256 and its signature by the test key."""
    folder = tmp / f"part-{name}-{version}"
    folder.mkdir(parents=True)
    manifest = {"name": name, "version": version, "mod": mod, "arch": arch, "env": {"PYTHONPATH": ["lib"]}, **info}
    (folder / "part.json").write_text(json.dumps(manifest))
    for path, content in (files or {"lib/kernel.py": "VALUE = 7\n"}).items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (folder / path).write_bytes(content)
        else:
            (folder / path).write_text(content)
    data = part.tarball(folder)
    return data, hashlib.sha256(data).hexdigest(), keys().sign(data)


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


KERNEL = Path(__file__).parent / "kernel"


def ada_part(tmp: Path, version: str = "3.10.0") -> tuple[bytes, str, str]:
    """The Ada part as the tests build it: the vendored kernel and its lexicon (the two seeds) under lib/ (numpy
    comes from the test's own Python)."""
    files: dict[str, str | bytes] = {
        f"lib/{name}": (KERNEL / name).read_text()
        for name in ("glyphh_kernel.py", "glyphh_spec.py", "glyphh_nsm.py", "glyphh_lexicon.py")
    }
    for seed in (KERNEL / "lexicon").iterdir():
        files[f"lib/lexicon/{seed.name}"] = seed.read_bytes()
    return part_archive(tmp / "ada-part", name="ada", version=version, mod="ada", files=files)


def _part_folders(path: Path) -> list[str]:
    try:
        return list(json.loads((path / "part.json").read_text()).get("env", {}).get("PYTHONPATH", []))
    except (OSError, ValueError):
        return []


class LocalLauncher:
    """The worker with no sandbox: the host's Python, the version's own folders. Tests only."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.specs: list[Spec] = []

    def sandbox(self, mounts: bwrap.Mounts) -> dict[str, Any] | None:
        return None

    def paths(self, mounts: bwrap.Mounts) -> dict[str, str | None]:
        return {"model": str(mounts.src), "files": str(mounts.files), "data": str(mounts.data) if mounts.data else None}

    async def spawn(self, spec: Spec) -> asyncio.subprocess.Process:
        self.specs.append(spec)
        if self.fail is not None:
            raise self.fail
        extra = [str(path / folder) for _, path in spec.mounts.parts for folder in _part_folders(path)]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": ":".join([str(Path(rrotor.__file__).parent.parent), *extra]),
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

    def __init__(self, configured: bool = True) -> None:
        self.seq, self.configured = 0, configured
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


def outcome(run: Any) -> dict[str, Any]:
    """A finished run's result: a test reads it only after the run has ended, so it is there."""
    assert run.result is not None, run
    return dict(run.result)


class FakeCollector:
    """An OTLP/HTTP receiver in this process: the customer's collector at /v1/<signal> and Glyphh's at
    /v1/otlp/v1/<signal>; each request kept as (signal, the decoded request, its headers). `down` answers 503."""

    def __init__(self) -> None:
        self.got: dict[str, list[tuple[str, Any, dict[str, str]]]] = {"collector": [], "glyphh": []}
        self.down = False
        self.server: Any = None

    async def start(self) -> str:
        from aiohttp import web
        from aiohttp.test_utils import TestServer

        from rrotord.otel import decoded

        async def take(request: web.Request) -> web.Response:
            if self.down:
                return web.Response(status=503)
            body = await request.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            signal = request.match_info["signal"]
            who = "glyphh" if request.path.startswith("/v1/otlp/") else "collector"
            self.got[who].append((signal, decoded(signal, body), dict(request.headers)))
            return web.Response(status=200, body=b"", content_type="application/x-protobuf")

        app = web.Application()
        app.router.add_post("/v1/{signal}", take)
        app.router.add_post("/v1/otlp/v1/{signal}", take)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server(access_log=None)
        return f"http://127.0.0.1:{self.server.port}"

    async def close(self) -> None:
        await self.server.close()

    def logs(self, who: str = "collector") -> list[dict[str, Any]]:
        """Every log record received: its body, event name, severity and attributes."""
        from rrotord.otel import attributes_of

        return [
            {
                "body": r.body.string_value,
                "event_name": r.event_name,
                "severity": r.severity_text,
                "trace_id": r.trace_id.hex(),
                **attributes_of(r.attributes),
            }
            for signal, req, _ in self.got[who]
            if signal == "logs"
            for rl in req.resource_logs
            for sl in rl.scope_logs
            for r in sl.log_records
        ]

    def spans(self, who: str = "collector") -> list[dict[str, Any]]:
        from rrotord.otel import attributes_of

        return [
            {
                "name": s.name,
                "trace_id": s.trace_id.hex(),
                "parent": s.parent_span_id.hex(),
                **attributes_of(s.attributes),
            }
            for signal, req, _ in self.got[who]
            if signal == "traces"
            for rs in req.resource_spans
            for ss in rs.scope_spans
            for s in ss.spans
        ]

    def metrics(self, who: str = "collector") -> set[str]:
        return {
            m.name
            for signal, req, _ in self.got[who]
            if signal == "metrics"
            for rm in req.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
        }

    def resources(self, who: str = "collector") -> list[dict[str, Any]]:
        from rrotord.otel import attributes_of

        found = []
        for signal, req, _ in self.got[who]:
            groups = {"logs": req.resource_logs if signal == "logs" else []}
            groups["traces"] = req.resource_spans if signal == "traces" else []
            groups["metrics"] = req.resource_metrics if signal == "metrics" else []
            found += [attributes_of(g.resource.attributes) for g in groups[signal]]
        return found

    def raw(self, who: str) -> bytes:
        """Everything received, serialized again: to grep for what must never be there."""
        return b"".join(req.SerializeToString() for _, req, _ in self.got[who])
