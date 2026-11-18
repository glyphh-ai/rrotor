"""The local API over HTTP: the real app (gate, errors, routes) with a real runner, deployer and credentials."""

from __future__ import annotations

import hashlib
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import rrotor
from rrotord.api.app import make
from rrotord.api.routes import Services
from rrotord.config import Config
from rrotord.deploy import Deployer, Tools
from rrotord.graph import Graph
from rrotord.licence import Licence
from rrotord.link.credentials import Credentials
from rrotord.link.handler import LinkHandler
from rrotord.parts import Parts
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.record import Records
from rrotord.scheduler import Scheduler
from rrotord.vault import Vault
from tests.fakes import FakeLink, LocalLauncher, keys, ready, token

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
    programs = Programs(config.data)
    layers = ready()
    parts = Parts(config.data, keys().public, "arm64")
    licence = Licence(config.data)
    runner = Runner(
        programs=programs,
        records=Records(config.data),
        proxy=EgressProxy(sock),
        launcher=LocalLauncher(),
        sdk=config.sdk,
        layers=layers,
        parts=parts,
        vault=Vault(config.data),
        graph=Graph(config.data),
    )
    deployer = Deployer(
        programs,
        root / "scratch",
        Tools(python=sys.executable, uv=shutil.which("uv") or "uv", github=f"file://{root / 'github'}"),
    )
    return Services(
        config,
        programs,
        deployer,
        runner,
        Credentials(config.data),
        LinkView(),
        layers,
        parts,
        licence,
        fetch,
        scheduler=Scheduler(programs, runner, config.data),
    )


def linked(api: Api) -> FakeLink:
    """The API's deploys reported through a real link handler to a fake link, as the daemon wires them."""
    s = api.services
    handler = LinkHandler(s.programs, s.runner, s.deployer)
    link = FakeLink()
    handler.link = cast(Any, link)
    s.report = handler.report
    return link


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
