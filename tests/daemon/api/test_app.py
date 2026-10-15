from __future__ import annotations

from typing import Any

from aiohttp import web

from rrotord.api.app import make
from rrotord.api.routes import BODY_MAX
from tests.daemon.api.conftest import services
from tests.fakes import token


def test_the_app_takes_a_bundle_sized_body(tmp_path, short_dir):
    app = make(services(tmp_path, short_dir, token()))
    assert app._client_max_size == BODY_MAX


def test_the_browser_part_adds_its_routes(tmp_path, short_dir):
    added: list[Any] = []

    class Browser:
        def routes(self, app: web.Application) -> None:
            added.append(app)

    app = make(services(tmp_path, short_dir, token(), Browser()))
    assert added == [app]
