"""The local API over HTTP: the real app (gate, errors, routes) with a real runner, deployer and credentials."""

from __future__ import annotations

import hashlib
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import rrotor
from rrotord.api.app import make
from rrotord.api.routes import Services
from rrotord.config import Config
from rrotord.deploy import Deployer, Tools
from rrotord.licence import Licence
from rrotord.link.credentials import Credentials
from rrotord.models import Models
from rrotord.parts import Parts
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.record import Records
from tests.fakes import LocalLauncher, keys, ready, token

PORT = 7777


class LinkView:
    def view(self) -> dict[str, Any]:
        return {"state": "no_token"}


@dataclass
class Api:
    client: TestClient[web.Request, web.Application]
    services: Services
    token: str
    root: Path

    def auth(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", **extra}


def services(root: Path, sock: Path, local_token: str, fetch: Any = None) -> Services:
    config = Config(
        data=root / "data",
        mode="local",
        server="https://glyphh.example.com",
        local_port=PORT,
        host_port=PORT,
        local_token_sha256=hashlib.sha256(local_token.encode()).hexdigest(),
        sdk=Path(rrotor.__file__).parent,
        run_dir=root / "run",
    )
    models = Models(config.data)
    layers = ready()
    parts = Parts(config.data, keys().public, "arm64")
    licence = Licence(config.data, keys().public)
    runner = Runner(
        models=models,
        records=Records(config.data),
        proxy=EgressProxy(sock),
        launcher=LocalLauncher(),
        sdk=config.sdk,
        layers=layers,
        parts=parts,
        licence=licence,
    )
    deployer = Deployer(
        models,
        root / "scratch",
        Tools(python=sys.executable, uv=shutil.which("uv") or "uv", github=f"file://{root / 'github'}"),
        maintain=runner.maintain,
    )
    return Services(
        config, models, deployer, runner, Credentials(config.data), LinkView(), layers, parts, licence, fetch
    )


async def open_api(root: Path, sock: Path, fetch: Any = None) -> Api:
    secret = token()
    s = services(root, sock, secret, fetch)
    client = TestClient(TestServer(make(s)), headers={"Host": f"127.0.0.1:{PORT}"})
    await client.start_server()
    return Api(client, s, secret, root)


@pytest.fixture
async def api(tmp_path: Path, short_dir: Path) -> AsyncIterator[Api]:
    a = await open_api(tmp_path, short_dir)
    yield a
    await a.services.runner.shutdown()
    await a.client.close()
