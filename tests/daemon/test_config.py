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
    c = config.load(
        {"RROTOR_SERVER": "http://localhost:8080", "RROTOR_MODE": "hosted", "FLY_MACHINE_ID": "3D8F1A2B3C4D5E"}
    )
    assert (c.data, c.local_port, c.host_port, c.local_token_sha256) == (Path("/data"), 7777, 7777, None)
    assert c.link_url == "ws://localhost:8080/v1/runtime/link"
    assert (c.runtime_id, c.runtime_token, c.builtin_parts) == (None, None, Path("/opt/parts-builtin"))
    assert (c.public_domain, c.machine_id, c.keys_sha256) == ("glyphh.app", "3d8f1a2b3c4d5e", ())


def test_a_hosted_rrotor_takes_its_runtime_credentials_and_built_in_parts_from_the_environment():
    env = {"RROTOR_SERVER": "http://s", "RROTOR_MODE": "hosted", "RROTOR_RUNTIME_ID": "r", "RROTOR_RUNTIME_TOKEN": "t"}
    env |= {
        "RROTOR_MACHINE_ID": "abc123",
        "RROTOR_KEYS_SHA256": f"{'A' * 64}, {'b' * 64}",
        "RROTOR_PUBLIC_DOMAIN": "Dev.Glyphh.App.",
    }
    c = config.load({**env, "RROTOR_BUILTIN_PARTS": "/parts"})
    assert (c.runtime_id, c.runtime_token, c.builtin_parts) == ("r", "t", Path("/parts"))
    assert (c.machine_id, c.keys_sha256, c.public_domain) == ("abc123", ("a" * 64, "b" * 64), "dev.glyphh.app")
    dev = config.load({"RROTOR_SERVER": "http://s", "RROTOR_MODE": "hosted", "RROTOR_PUBLIC_HOST": "X.ngrok-free.app."})
    assert (dev.machine_id, dev.public_host) == (None, "x.ngrok-free.app")
    doorman = config.load({"RROTOR_MODE": "doorman"})
    assert (doorman.mode, doorman.server, doorman.public_domain) == ("doorman", "http://doorman.invalid", "glyphh.app")


SELF = {"RROTOR_MODE": "self", "RROTOR_RUNTIME_ID": "r", "RROTOR_RUNTIME_TOKEN": "t"} | {
    "RROTOR_PUBLIC_HOST": "Rr.Acme.com."
}


def test_a_self_hosted_rrotor_answers_at_its_public_host_with_glyphhs_server_as_the_default():
    c = config.load(SELF)
    assert (c.mode, c.server, c.public_host, c.runtime_id, c.runtime_token) == (
        "self",
        "https://api.glyphh.ai",
        "rr.acme.com",
        "r",
        "t",
    )
    assert c.public and c.link and c.licence_token is None and c.link_url == "wss://api.glyphh.ai/v1/runtime/link"
    off = config.load(
        SELF | {"RROTOR_SERVER": "https://glyphh.acme.com", "RROTOR_LINK": "OFF", "RROTOR_LICENCE": " t "}
    )
    assert (off.server, off.link, off.licence_token) == ("https://glyphh.acme.com", False, "t")
    assert config.load({**SELF, "RROTOR_SERVER": ""}).server == "https://api.glyphh.ai"
    assert not config.load({"RROTOR_SERVER": "http://s", "RROTOR_LOCAL_TOKEN_SHA256": DIGEST}).public


def test_load_from_the_process_environment(monkeypatch):
    monkeypatch.setenv("RROTOR_SERVER", "http://s")
    monkeypatch.setenv("RROTOR_MODE", "hosted")
    monkeypatch.setenv("FLY_MACHINE_ID", "abc123")
    assert config.load().server == "http://s"


@pytest.mark.parametrize(
    "env",
    [
        {"RROTOR_MODE": "cloud", "RROTOR_SERVER": "http://s"},
        {"RROTOR_SERVER": "ftp://s", "RROTOR_LOCAL_TOKEN_SHA256": DIGEST},
        {"RROTOR_SERVER": "http://s/path", "RROTOR_LOCAL_TOKEN_SHA256": DIGEST},
        {"RROTOR_SERVER": "http://s", "RROTOR_LOCAL_TOKEN_SHA256": "XYZ"},
        {"RROTOR_SERVER": "http://s"},
        {"RROTOR_SERVER": "http://s", "RROTOR_MODE": "hosted", "RROTOR_RUNTIME_TOKEN": "t", "FLY_MACHINE_ID": "a"},
        {"RROTOR_SERVER": "http://s", "RROTOR_MODE": "hosted"},
        {"RROTOR_SERVER": "http://s", "RROTOR_MODE": "hosted", "FLY_MACHINE_ID": "a", "RROTOR_KEYS_SHA256": "zz"},
        {**SELF, "RROTOR_RUNTIME_TOKEN": ""},
        {**SELF, "RROTOR_RUNTIME_ID": ""},
        {**SELF, "RROTOR_PUBLIC_HOST": ""},
        {**SELF, "RROTOR_PUBLIC_HOST": "https://rr.acme.com/"},
        {**SELF, "RROTOR_LINK": "maybe"},
    ],
)
def test_load_refuses_bad_settings(env):
    with pytest.raises(RrotorError) as caught:
        config.load(env)
    assert caught.value.code == "E_CONFIG"
