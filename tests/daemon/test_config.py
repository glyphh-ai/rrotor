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


RUNTIME = "6F1C2A9E-0000-4000-8000-000000000001"
SELF = {"RROTOR_MODE": "self", "RROTOR_RUNTIME_ID": RUNTIME, "RROTOR_RUNTIME_TOKEN": "t"} | {
    "RROTOR_PUBLIC_HOST": "Rr.Acme.com."
}


def test_a_self_hosted_rrotor_answers_at_its_public_host_with_glyphhs_server_as_the_default():
    c = config.load(SELF)
    assert (c.mode, c.server, c.public_host, c.runtime_id, c.runtime_token) == (
        "self",
        "https://api.glyphh.ai",
        "rr.acme.com",
        RUNTIME.lower(),
        "t",
    )
    assert c.public and c.link and c.licence_env == () and c.link_url == "wss://api.glyphh.ai/v1/runtime/link"
    off = config.load(
        SELF | {"RROTOR_SERVER": "https://glyphh.acme.com", "RROTOR_LINK": "OFF", "RROTOR_LICENCE": " t "}
    )
    assert (off.server, off.link, off.licence_env) == ("https://glyphh.acme.com", False, (("RROTOR_LICENCE", "t"),))
    assert config.load({**SELF, "RROTOR_SERVER": ""}).server == "https://api.glyphh.ai"
    assert not config.load({"RROTOR_SERVER": "http://s", "RROTOR_LOCAL_TOKEN_SHA256": DIGEST}).public


def test_the_licence_key_comes_from_glyphh_license_key_or_its_alias_and_no_public_key_from_the_environment():
    c = config.load(SELF | {"GLYPHH_LICENSE_KEY": " a.b\n", "RROTOR_LICENCE": "c.d", "RROTOR_LICENCE_PUBLIC_KEY": "x"})
    assert c.licence_env == (("GLYPHH_LICENSE_KEY", "a.b"), ("RROTOR_LICENCE", "c.d"))
    assert not hasattr(c, "licence_key")  # the licence's public key is the code's alone


def test_the_node_id_is_rrotor_node_id_or_the_hostname_made_one(monkeypatch):
    assert config.load(SELF | {"RROTOR_NODE_ID": "rrotor-0"}).node_id == "rrotor-0"
    monkeypatch.setattr("socket.gethostname", lambda: "Build_Box.Acme.com")
    assert config.load(SELF).node_id == "build-box"
    monkeypatch.setattr("socket.gethostname", lambda: "")
    assert config.load(SELF).node_id == "rrotor"


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
        {**SELF, "RROTOR_RUNTIME_ID": "r"},
        {**SELF, "RROTOR_NODE_ID": "Rrotor_1"},
        {**SELF, "RROTOR_NODE_ID": "a" * 64},
    ],
)
def test_load_refuses_bad_settings(env):
    with pytest.raises(RrotorError) as caught:
        config.load(env)
    assert caught.value.code == "E_CONFIG"
