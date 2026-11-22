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
    assert c.platform_origin == "https://platform.glyphh.ai"
    assert c.folders == ()  # no folder of the machine is offered until its owner names one


def test_the_folders_the_owner_offers_are_read(tmp_path):
    local = {"RROTOR_SERVER": "http://s", "RROTOR_LOCAL_TOKEN_SHA256": DIGEST, "RROTOR_DATA": str(tmp_path / "data")}
    docs, work = tmp_path.resolve() / "docs", tmp_path.resolve() / "work"
    docs.mkdir()
    assert config.load({**local, "RROTOR_FOLDERS": f"{docs}:{work}"}).folders == (docs, work)
    for bad in ("docs", str(tmp_path / "data"), "/"):
        with pytest.raises(RrotorError) as caught:
            config.load({**local, "RROTOR_FOLDERS": bad})
        assert caught.value.code == "E_CONFIG" and "RROTOR_FOLDERS" in caught.value.message, bad


def test_the_platforms_origin_is_one_origin_and_may_be_set():
    env = {"RROTOR_SERVER": "http://s", "RROTOR_LOCAL_TOKEN_SHA256": DIGEST}
    assert config.load(env | {"RROTOR_PLATFORM_ORIGIN": " http://localhost:5173/ "}).platform_origin == (
        "http://localhost:5173"
    )
    for bad in ("platform.glyphh.ai", "https://platform.glyphh.ai/bots", "*"):
        with pytest.raises(RrotorError) as err:
            config.load(env | {"RROTOR_PLATFORM_ORIGIN": bad})
        assert err.value.code == "E_CONFIG" and "RROTOR_PLATFORM_ORIGIN" in err.value.message


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
        {**SELF, "RROTOR_AUDIT_RETENTION_DAYS": "0"},
        {**SELF, "RROTOR_AUDIT_MAX_MB": "lots"},
        {**SELF, "RROTOR_DRAIN_BUFFER": "-1"},
        {**SELF, "RROTOR_DRAIN_SIGNALS": "logs,events"},
        {**SELF, "RROTOR_DRAIN_SIGNALS": ","},
        {**SELF, "RROTOR_DRAIN_AT": "everywhere"},
        {**SELF, "RROTOR_LOG_PAYLOADS": "yes"},
        {**SELF, "RROTOR_SERVER_LOGS": "on", "RROTOR_LINK": "off"},
        {**SELF, "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"},
        {**SELF, "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json"},
        {**SELF, "OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4318"},
        {**SELF, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://c 1/v1/traces"},
        {**SELF, "OTEL_TRACES_SAMPLER": "jaeger_remote"},
        {**SELF, "OTEL_TRACES_SAMPLER_ARG": "half"},
        {**SELF, "OTEL_TRACES_SAMPLER_ARG": "2"},
    ],
)
def test_load_refuses_bad_settings(env):
    with pytest.raises(RrotorError) as caught:
        config.load(env)
    assert caught.value.code == "E_CONFIG"


def test_logging_audit_and_drain_settings_default_to_everything_kept_here():
    c = config.load(SELF)
    assert (c.audit_retention_days, c.audit_max_mb, c.drain_buffer) == (90, 1024, 10000)
    assert (c.drain_signals, c.drain_at, c.server_logs, c.log_payloads, c.otel) == (
        ("logs", "metrics", "traces", "audit"),
        "head",
        False,
        False,
        (),
    )
    assert [c.endpoint(s) for s in ("logs", "metrics", "traces")] == [None, None, None]


def test_logging_audit_and_drain_settings_are_read():
    c = config.load(
        {
            **SELF,
            "RROTOR_AUDIT_RETENTION_DAYS": "30",
            "RROTOR_AUDIT_MAX_MB": "64",
            "RROTOR_DRAIN_BUFFER": "500",
            "RROTOR_DRAIN_SIGNALS": " Logs, audit,logs ",
            "RROTOR_DRAIN_AT": "node",
            "RROTOR_SERVER_LOGS": "ON",
            "RROTOR_LOG_PAYLOADS": "on",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.acme.com:4318/",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "https://metrics.acme.com/ingest",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_TRACES_SAMPLER": "traceidratio",
            "OTEL_TRACES_SAMPLER_ARG": "0.1",
            "OTEL_SERVICE_NAME": " ",
            "PATH": "/bin",
        }
    )
    assert (c.audit_retention_days, c.audit_max_mb, c.drain_buffer) == (30, 64, 500)
    assert (c.drain_signals, c.drain_at, c.server_logs, c.log_payloads) == (("logs", "audit"), "node", True, True)
    assert [c.endpoint(s) for s in ("logs", "metrics", "traces")] == [
        "https://otel.acme.com:4318/v1/logs",
        "https://metrics.acme.com/ingest",
        "https://otel.acme.com:4318/v1/traces",
    ]
    assert c.otel_env["OTEL_TRACES_SAMPLER_ARG"] == "0.1" and "OTEL_SERVICE_NAME" not in c.otel_env


SECRET_TEXT = "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldA"  # 34 bytes, URL-safe base64 unpadded
CLUSTER = SELF | {"RROTOR_CLUSTER": "on", "RROTOR_CLUSTER_SECRET": SECRET_TEXT, "RROTOR_NODE_ID": "rrotor-1"}


def test_the_cluster_is_off_by_default_with_its_settings_defaulted():
    c = config.load(SELF | {"RROTOR_NODE_ID": "rrotor-1"})
    assert (c.cluster, c.cluster_peers, c.cluster_dns, c.cluster_port, c.cluster_secrets) == (False, (), None, 7778, ())
    assert (c.cluster_head, c.cluster_log_keep, c.cluster_evict_after) == (None, 1 << 30, 86400.0)
    assert c.advertise == "rrotor-1:7778" and "cluster_secrets" not in repr(c)


def test_the_cluster_settings_are_read():
    c = config.load(
        CLUSTER
        | {
            "RROTOR_CLUSTER_PEERS": "rrotor-0:7778, rrotor-1:7778,[fd00::2]:7778",
            "RROTOR_CLUSTER_PORT": "7800",
            "RROTOR_CLUSTER_ADVERTISE": "10.0.0.5:7800",
            "RROTOR_CLUSTER_SECRET_PREVIOUS": "b2xkLW9sZC1vbGQtb2xkLW9sZC1vbGQtb2xkLW9sZC1vbGQ=",
            "RROTOR_CLUSTER_HEAD": "rrotor-0",
            "RROTOR_CLUSTER_LOG_KEEP": "512m",
            "RROTOR_CLUSTER_EVICT_AFTER": "90m",
        }
    )
    assert c.cluster and c.cluster_peers == ("rrotor-0:7778", "rrotor-1:7778", "[fd00::2]:7778")
    assert (c.cluster_port, c.advertise, c.cluster_head) == (7800, "10.0.0.5:7800", "rrotor-0")
    assert [len(s) for s in c.cluster_secrets] == [34, 35] and c.cluster_secrets[1].startswith(b"old-")
    assert (c.cluster_log_keep, c.cluster_evict_after) == (512 << 20, 5400.0)
    dns = config.load(CLUSTER | {"RROTOR_CLUSTER_DNS": "Rrotor-Headless.rrotor.svc.cluster.local."})
    assert (dns.cluster_dns, dns.cluster_peers) == ("rrotor-headless.rrotor.svc.cluster.local", ())
    assert (
        config.load(CLUSTER | {"RROTOR_CLUSTER_DNS": "x", "RROTOR_CLUSTER_LOG_KEEP": "4096"}).cluster_log_keep == 4096
    )


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"RROTOR_CLUSTER": "yes"}, "RROTOR_CLUSTER"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_MODE": "local"}, "RROTOR_MODE=self"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_PORT": "0"}, "RROTOR_CLUSTER_PORT"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_PORT": "x"}, "RROTOR_CLUSTER_PORT"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_PORT": "7777"}, "the API's port"),
        ({"RROTOR_CLUSTER_PEERS": "a"}, "RROTOR_CLUSTER_PEERS"),
        ({"RROTOR_CLUSTER_PEERS": "a:99999"}, "RROTOR_CLUSTER_PEERS"),
        ({"RROTOR_CLUSTER_PEERS": "bad host:1"}, "RROTOR_CLUSTER_PEERS"),
        ({"RROTOR_CLUSTER_DNS": "bad name"}, "RROTOR_CLUSTER_DNS"),
        ({}, "one of RROTOR_CLUSTER_PEERS"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_DNS": "b"}, "one of RROTOR_CLUSTER_PEERS"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_ADVERTISE": "nope"}, "RROTOR_CLUSTER_ADVERTISE"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_SECRET": ""}, "needs RROTOR_CLUSTER_SECRET"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_SECRET": "c2hvcnQ="}, "RROTOR_CLUSTER_SECRET is 32"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_SECRET": "!!not base64!!"}, "RROTOR_CLUSTER_SECRET is"),
        ({"RROTOR_CLUSTER": "off", "RROTOR_CLUSTER_SECRET": "", "RROTOR_CLUSTER_SECRET_PREVIOUS": SECRET_TEXT}, "goes"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_HEAD": "Head_0"}, "RROTOR_CLUSTER_HEAD"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_LOG_KEEP": "1t"}, "RROTOR_CLUSTER_LOG_KEEP"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_LOG_KEEP": "0g"}, "RROTOR_CLUSTER_LOG_KEEP"),
        ({"RROTOR_CLUSTER_PEERS": "a:1", "RROTOR_CLUSTER_EVICT_AFTER": "soon"}, "RROTOR_CLUSTER_EVICT_AFTER"),
    ],
)
def test_bad_cluster_settings_stop_rrotor_naming_them(env, named):
    given = CLUSTER | env
    if given.get("RROTOR_MODE") == "local":
        given |= {"RROTOR_LOCAL_TOKEN_SHA256": DIGEST, "RROTOR_SERVER": "http://s"}
    with pytest.raises(RrotorError) as err:
        config.load(given)
    assert err.value.code == "E_CONFIG" and named in err.value.message
