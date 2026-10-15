from pathlib import Path

import pytest

import rrotor
from rrotord import config
from rrotord.errors import RrotorError

DIGEST = "ab" * 32


def test_load_reads_the_environment():
    c = config.load(
        {
            "RROTOR_SERVER": "https://glyphh.example.com/",
            "RROTOR_LOCAL_TOKEN_SHA256": DIGEST,
            "RROTOR_DATA": "/d",
            "RROTOR_LOCAL_PORT": "8000",
            "RROTOR_HOST_PORT": "9000",
            "RROTOR_RUN_DIR": "/r",
        }
    )
    assert (c.data, c.mode, c.server, c.local_port, c.host_port) == (
        Path("/d"),
        "local",
        "https://glyphh.example.com",
        8000,
        9000,
    )
    assert (c.local_token_sha256, c.sdk, c.run_dir) == (DIGEST, Path(rrotor.__file__).parent, Path("/r"))
    assert c.link_url == "wss://glyphh.example.com/v1/runtime/link"


def test_load_defaults():
    c = config.load({"RROTOR_SERVER": "http://localhost:8080", "RROTOR_MODE": "hosted"})
    assert (c.data, c.local_port, c.host_port, c.local_token_sha256) == (Path("/data"), 7777, 7777, None)
    assert c.link_url == "ws://localhost:8080/v1/runtime/link"


def test_load_from_the_process_environment(monkeypatch):
    monkeypatch.setenv("RROTOR_SERVER", "http://s")
    monkeypatch.setenv("RROTOR_MODE", "hosted")
    assert config.load().server == "http://s"


@pytest.mark.parametrize(
    "env",
    [
        {"RROTOR_MODE": "cloud", "RROTOR_SERVER": "http://s"},
        {"RROTOR_SERVER": "ftp://s", "RROTOR_LOCAL_TOKEN_SHA256": DIGEST},
        {"RROTOR_SERVER": "http://s/path", "RROTOR_LOCAL_TOKEN_SHA256": DIGEST},
        {"RROTOR_SERVER": "http://s", "RROTOR_LOCAL_TOKEN_SHA256": "XYZ"},
        {"RROTOR_SERVER": "http://s"},
    ],
)
def test_load_refuses_bad_settings(env):
    with pytest.raises(RrotorError) as caught:
        config.load(env)
    assert caught.value.code == "E_CONFIG"
