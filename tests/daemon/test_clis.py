"""CLIs' install and run helpers (rrotord/clis.py): every source from archives made here, through a fake fetcher (no
network); the HTTPS fetcher against a fake session; the argument patterns, inputs and output files."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from rrotord import clis
from rrotord.errors import BadRequest, DeployError
from rrotord.manifest import Manifest
from tests.fakes import ECHO, FakeFetch, cli_manifest, console_wheel, sha, tarball, zipped

URL = "https://example.com/tool.tar.gz"


def manifest(cli: dict[str, Any] | None = None, **extra: Any) -> Manifest:
    return Manifest.model_validate(cli_manifest(cli=cli, **extra))


async def install(tmp_path: Path, found: Manifest, fetch: FakeFetch, src: Path | None = None) -> clis.Installed:
    source = src or tmp_path / "src"
    source.mkdir(exist_ok=True)
    return await clis.Installer(fetch, python=sys.executable, uv=shutil.which("uv") or "uv").install(
        found, tmp_path / "cli", source
    )


def failure(caught: pytest.ExceptionInfo[DeployError]) -> tuple[str, str]:
    return caught.value.code, caught.value.message


# From a release URL


async def test_a_tarball_is_checked_unpacked_and_its_command_found(tmp_path):
    archive = tarball({"tool-1.0/bin/tool": ECHO, "tool-1.0/README": "x"})
    fetch = FakeFetch({URL: archive})
    made = await install(tmp_path, manifest({"install": {"url": URL, "sha256": sha(archive)}}), fetch)
    assert made == clis.Installed("exec", "files/tool-1.0/bin/tool")
    assert (tmp_path / "cli" / made.path).stat().st_mode & 0o111
    assert clis.installed(tmp_path / "cli") == made
    assert fetch.asked == [(URL, frozenset({"example.com"}))]
    assert made.where("/cli") == "/cli/files/tool-1.0/bin/tool"


async def test_a_wrong_checksum_is_refused_before_anything_is_unpacked(tmp_path):
    archive = tarball({"bin/tool": ECHO})
    found = manifest({"install": {"url": URL, "sha256": "0" * 64}})
    with pytest.raises(DeployError) as caught:
        await install(tmp_path, found, FakeFetch({URL: archive}))
    code, message = failure(caught)
    assert code == "E_CLI_CHECKSUM" and sha(archive) in message and "0" * 64 in message
    assert list((tmp_path / "cli").iterdir()) == []


async def test_a_zip_keeps_its_executables_and_a_path_command(tmp_path):
    url = "https://example.com/tool.zip"
    archive = zipped({"pkg/bin/tool": ECHO, "pkg/doc/": "", "pkg/lib/data.txt": "x"}, frozenset({"pkg/bin/tool"}))
    found = manifest({"install": {"url": url, "sha256": sha(archive)}, "command": "pkg/bin/tool"})
    made = await install(tmp_path, found, FakeFetch({url: archive}))
    assert made.path == "files/pkg/bin/tool" and (tmp_path / "cli" / made.path).stat().st_mode & 0o100


async def test_a_bare_binary_is_the_command(tmp_path):
    url = "https://example.com/download/tool-linux-amd64"
    found = manifest({"install": {"url": url, "sha256": sha(ECHO.encode())}})
    made = await install(tmp_path, found, FakeFetch({url: ECHO.encode()}))
    assert made.path == "files/tool" and (tmp_path / "cli" / "files" / "tool").read_text() == ECHO


@pytest.mark.parametrize(
    ("files", "url", "why"),
    [
        ({"bin/other": "x"}, URL, "The install has no tool"),
        ({"../escape/tool": "x"}, "https://example.com/t.zip", "outside its folder"),
    ],
)
async def test_an_archive_without_the_command_or_escaping_is_refused(tmp_path, files, url, why):
    archive = zipped(files) if url.endswith(".zip") else tarball(files)
    found = manifest({"install": {"url": url, "sha256": sha(archive)}})
    with pytest.raises(DeployError, match=why):
        await install(tmp_path, found, FakeFetch({url: archive}))


async def test_a_tarball_member_outside_its_folder_is_refused(tmp_path):
    archive = tarball({"../tool": "x"})
    with pytest.raises(DeployError, match="does not unpack"):
        await install(tmp_path, manifest({"install": {"url": URL, "sha256": sha(archive)}}), FakeFetch({URL: archive}))


@pytest.mark.parametrize("url", [URL, "https://example.com/t.zip"])
async def test_an_archive_that_is_not_one_is_refused(tmp_path, url):
    junk = b"not an archive"
    with pytest.raises(DeployError, match="does not unpack"):
        await install(tmp_path, manifest({"install": {"url": url, "sha256": sha(junk)}}), FakeFetch({url: junk}))


@pytest.mark.parametrize("url", [URL, "https://example.com/t.zip"])
async def test_an_archive_too_big_unpacked_is_refused(tmp_path, monkeypatch, url):
    monkeypatch.setattr(clis, "UNPACKED_MAX", 3)
    archive = zipped({"tool": "four"}) if url.endswith(".zip") else tarball({"tool": "four"})
    with pytest.raises(DeployError, match="unpacks to over"):
        await install(tmp_path, manifest({"install": {"url": url, "sha256": sha(archive)}}), FakeFetch({url: archive}))


async def test_a_host_the_manifest_does_not_imply_is_refused(tmp_path):
    fetch = FakeFetch()
    with pytest.raises(DeployError, match="not in the CLI's hosts"):
        await fetch("https://evil.example.org/x", frozenset({"example.com"}))
    with pytest.raises(DeployError, match="is not https on 443"):
        await fetch("http://example.com/x", frozenset({"example.com"}))


# From npm


def npm_package(package: dict[str, Any], files: dict[str, str] | None = None) -> bytes:
    return tarball({"package/package.json": json.dumps(package), **(files or {"package/cli.js": "console.log(1)"})})


NPM_URL = "https://registry.npmjs.org/@scope/tool/-/tool-1.2.3.tgz"


@pytest.mark.parametrize(
    "package",
    [
        {"name": "@scope/tool", "bin": {"tool": "./cli.js"}},
        {"name": "@scope/tool", "bin": "cli.js", "dependencies": {"x": "1"}, "bundleDependencies": ["x"]},
    ],
)
async def test_an_npm_package_runs_its_bin_with_node(tmp_path, package):
    archive = npm_package(package)
    found = manifest({"install": {"npm": "@scope/tool@1.2.3", "sha256": sha(archive)}})
    fetch = FakeFetch({NPM_URL: archive})
    assert await install(tmp_path, found, fetch) == clis.Installed("node", "files/package/cli.js")
    assert fetch.asked[0][1] == frozenset({"registry.npmjs.org"})


@pytest.mark.parametrize(
    ("package", "files", "why"),
    [
        ({"bin": {"tool": "cli.js"}, "dependencies": {"left-pad": "1"}}, None, "depends on left-pad, not bundled"),
        ({"bin": {"other": "cli.js"}}, None, r"has no bin tool \(it has: other\)"),
        ({}, None, r"has no bin tool \(it has: none\)"),
        (None, {"package/cli.js": "x"}, "has no package/package.json"),
    ],
)
async def test_an_npm_package_that_cannot_run_is_refused(tmp_path, package, files, why):
    archive = tarball({"package/package.json": json.dumps(package), **(files or {})} if package is not None else files)
    found = manifest({"install": {"npm": "@scope/tool@1.2.3", "sha256": sha(archive)}})
    with pytest.raises(DeployError, match=why):
        await install(tmp_path, found, FakeFetch({NPM_URL: archive}))


# From PyPI


LISTING = "https://pypi.org/pypi/hello/1.0/json"


def listing(wheel: bytes, url: str = "https://files.pythonhosted.org/hello-1.0-py3-none-any.whl") -> bytes:
    other = {"packagetype": "sdist", "digests": {"sha256": sha(wheel)}, "url": "https://files.pythonhosted.org/s"}
    return json.dumps(
        {"urls": [other, {"packagetype": "bdist_wheel", "digests": {"sha256": sha(wheel)}, "url": url}]}
    ).encode()


async def test_a_pypi_wheel_is_found_by_its_checksum_unpacked_and_its_script_written(tmp_path):
    wheel = console_wheel()
    found = manifest({"install": {"pypi": "hello==1.0", "sha256": sha(wheel)}, "command": "hello"})
    fetch = FakeFetch({LISTING: listing(wheel), "https://files.pythonhosted.org/hello-1.0-py3-none-any.whl": wheel})
    made = await install(tmp_path, found, fetch)
    assert made == clis.Installed("python", "bin/hello")
    site = tmp_path / "cli" / "site"
    assert (site / "hello" / "__init__.py").is_file() and (site / "hello_extra.py").is_file()
    assert not list(site.rglob("ignored"))
    assert [h for _, h in fetch.asked] == [frozenset({"pypi.org", "files.pythonhosted.org"})] * 2
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(tmp_path / "cli" / made.path), "a", "b", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    assert (proc.returncode, out) == (0, b"hello a b\n")


async def test_a_pypi_cli_installs_its_lock_into_its_site(tmp_path):
    from tests.daemon.test_deploy import wheel as tiny

    path, digest = tiny(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "requirements.lock").write_text(f"tinypkg @ {path.as_uri()} --hash=sha256:{digest}\n")
    wheel = console_wheel()
    found = manifest({"install": {"pypi": "hello==1.0", "sha256": sha(wheel)}, "command": "hello"})
    fetch = FakeFetch({LISTING: listing(wheel), "https://files.pythonhosted.org/hello-1.0-py3-none-any.whl": wheel})
    await install(tmp_path, found, fetch, src)
    assert (tmp_path / "cli" / "site" / "tinypkg" / "__init__.py").is_file()
    (src / "requirements.lock").write_text(f"tinypkg @ {path.as_uri()} --hash=sha256:{'0' * 64}\n")
    shutil.rmtree(tmp_path / "cli")
    with pytest.raises(DeployError) as caught:
        await install(tmp_path, found, fetch, src)
    assert caught.value.code == "E_DEPS"


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (b"not json", "PyPI has no wheel of hello==1.0"),
        (json.dumps({"urls": []}).encode(), "PyPI has no wheel of hello==1.0"),
    ],
)
async def test_pypi_without_that_wheel_is_refused(tmp_path, answer, why):
    found = manifest({"install": {"pypi": "hello==1.0", "sha256": "b" * 64}, "command": "hello"})
    with pytest.raises(DeployError, match=why) as caught:
        await install(tmp_path, found, FakeFetch({LISTING: answer}))
    assert caught.value.code == "E_CLI_CHECKSUM"


@pytest.mark.parametrize(
    ("entry_points", "why"),
    [
        ("[console_scripts]\nother = hello:main\n", r"no console script hello \(it has: other\)"),
        ("not ini", r"no console script hello \(it has: none\)"),
        ("[console_scripts]\nhello = hello:main; import os\n", "not module:function"),
    ],
)
async def test_a_wheel_without_that_console_script_is_refused(tmp_path, entry_points, why):
    wheel = zipped({"hello/__init__.py": "", "hello-1.0.dist-info/entry_points.txt": entry_points})
    found = manifest({"install": {"pypi": "hello==1.0", "sha256": sha(wheel)}, "command": "hello"})
    fetch = FakeFetch({LISTING: listing(wheel), "https://files.pythonhosted.org/hello-1.0-py3-none-any.whl": wheel})
    with pytest.raises(DeployError, match=why):
        await install(tmp_path, found, fetch)


# From the image


async def test_an_image_binary_is_found_on_path_and_checked(tmp_path):
    sh = Path(shutil.which("sh", path=clis.IMAGE_PATH) or "/bin/sh")
    found = manifest({"install": {"image": "sh", "sha256": sha(sh.read_bytes())}, "command": "sh"})
    made = await install(tmp_path, found, FakeFetch())
    assert made == clis.Installed("exec", str(sh), absolute=True) and made.where("/cli") == str(sh)


@pytest.mark.parametrize(
    ("image", "elsewhere", "why"),
    [
        ("nope", False, "The image has no nope"),
        ("tool", True, "outside /usr"),
        ("/usr/bin/no-such-binary-here", False, "The image has no"),
    ],
)
async def test_an_image_binary_not_there_or_outside_usr_is_refused(tmp_path, image, elsewhere, why):
    target = tmp_path / "tool"  # a file, but not under /usr: the sandbox would not have it
    target.write_text("x")
    command = image.rsplit("/", 1)[-1]
    found = manifest({"install": {"image": image}, "command": command})

    def find(name: str, path: str) -> str | None:
        return str(target) if elsewhere else None

    with pytest.raises(DeployError, match=why):
        await clis.Installer(FakeFetch(), which=find).install(found, tmp_path / "cli", tmp_path)


def test_installed_without_a_record_is_none(tmp_path):
    assert clis.installed(tmp_path) is None
    (tmp_path / clis.INSTALLED).write_text("{}")
    assert clis.installed(tmp_path) is None


def test_the_installer_downloads_with_https_by_default():
    assert isinstance(clis.Installer().fetch, clis.HttpsFetch)


# The HTTPS fetcher


class Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _: int) -> Any:
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, status: int, chunks: list[bytes] | None = None, headers: dict[str, str] | None = None) -> None:
        self.status, self.headers, self.content = status, headers or {}, Content(chunks or [])

    async def __aenter__(self) -> Response:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class Session:
    def __init__(self, answers: dict[str, Response | BaseException]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def get(self, url: str, allow_redirects: bool) -> Response:
        assert allow_redirects is False
        self.asked.append(url)
        found = self.answers[url]
        if isinstance(found, BaseException):
            raise found
        return found

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


HOSTS = frozenset({"github.com", "objects.githubusercontent.com"})


async def test_the_fetcher_follows_redirects_to_allowed_hosts_only():
    start = "https://github.com/o/r/releases/download/v1/t.tgz"
    session = Session(
        {
            start: Response(302, headers={"Location": "https://objects.githubusercontent.com/a?sig=1"}),
            "https://objects.githubusercontent.com/a?sig=1": Response(200, [b"ab", b"cd"]),
        }
    )
    assert await clis.HttpsFetch(lambda: session)(start, HOSTS) == b"abcd"
    elsewhere = Session({start: Response(301, headers={"Location": "https://evil.example.org/x"})})
    with pytest.raises(DeployError, match=r"evil\.example\.org is not in the CLI's hosts"):
        await clis.HttpsFetch(lambda: elsewhere)(start, HOSTS)


@pytest.mark.parametrize(
    ("answers", "why"),
    [
        ({"https://github.com/x": Response(404)}, "answered 404"),
        ({"https://github.com/x": Response(302)}, "answered 302"),  # a redirect with nowhere to go
        ({"https://github.com/x": Response(200, [b"12345"])}, "is over 0 MB"),
        ({"https://github.com/x": OSError("refused")}, "could not be downloaded from: refused"),
        ({"https://github.com/x": Response(302, headers={"Location": "/x"})}, "redirects more than 5 times"),
    ],
)
async def test_the_fetcher_refuses_what_it_cannot_take(monkeypatch, answers, why):
    monkeypatch.setattr(clis, "DOWNLOAD_MAX", 4)
    with pytest.raises(DeployError, match=why):
        await clis.HttpsFetch(lambda: Session(answers))("https://github.com/x", HOSTS)


async def test_the_default_session_resolves_public_addresses_only():
    session = clis._session()
    try:
        assert isinstance(session.connector._resolver, clis.PublicResolver)  # type: ignore[union-attr]
    finally:
        await session.close()


class Inner:
    def __init__(self, hosts: list[str]) -> None:
        self.hosts, self.closed = hosts, False

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[Any]:
        return [
            {"host": h, "hostname": host, "port": port, "family": family, "proto": 0, "flags": 0} for h in self.hosts
        ]

    async def close(self) -> None:
        self.closed = True


async def test_the_public_resolver_drops_private_addresses():
    inner = Inner(["10.0.0.1", "140.82.112.3"])
    resolver = clis.PublicResolver(inner)  # type: ignore[arg-type]
    assert [r["host"] for r in await resolver.resolve("github.com", 443)] == ["140.82.112.3"]
    with pytest.raises(OSError, match="only to private or local"):
        await clis.PublicResolver(Inner(["127.0.0.1"])).resolve("evil.example.org")  # type: ignore[arg-type]
    await resolver.close()
    assert inner.closed
    assert isinstance(clis.PublicResolver().inner, aiohttp.ThreadedResolver)


# Running: patterns, inputs, output files


def test_patterns_match_the_arguments_as_a_shell_would_quote_them():
    assert clis.command_line(["pr", "view", "a b"]) == "pr view 'a b'"
    assert clis.allowed([], ["anything"]) and clis.allowed(["pr *"], ["pr", "list"])
    assert not clis.allowed(["pr *"], ["repo", "delete"]) and not clis.allowed(["pr *"], [])
    assert not clis.allowed(["pr view *"], ["pr view", "x"])  # one argument with a space is quoted: no match
    assert clis.matching(["pr merge *", "pr *"], ["pr", "merge", "1"]) == "pr merge *"
    assert clis.matching(["pr merge *"], ["pr", "list"]) is None


@pytest.mark.parametrize(
    "args", [None, "pr list", [1], ["x"] * (clis.ARGS_MAX + 1), ["a" * (clis.ARG_MAX + 1)], ["nul\x00"]]
)
def test_bad_args_are_refused(args):
    with pytest.raises(BadRequest) as caught:
        clis.check_args(args)
    assert caught.value.code == "E_CLI_INPUTS"


def test_stdin_is_text_and_capped(monkeypatch):
    assert clis.check_stdin(None) is None and clis.check_stdin("hi") == "hi"
    monkeypatch.setattr(clis, "STDIN_MAX", 2)
    for bad in (3, "abc"):
        with pytest.raises(BadRequest, match="stdin is text"):
            clis.check_stdin(bad)


def test_files_are_text_or_base64_by_relative_path():
    found = clis.check_files({"a.txt": "hi", "d/b.bin": {"base64": base64.b64encode(b"\x00\x01").decode()}})
    assert found == {"a.txt": b"hi", "d/b.bin": b"\x00\x01"} and clis.check_files(None) == {}


@pytest.mark.parametrize(
    ("files", "why"),
    [
        ("x", "files is an object"),
        ({"/abs": "x"}, "not a relative path"),
        ({"../up": "x"}, "not a relative path"),
        ({"": "x"}, "not a relative path"),
        ({"a": 1}, "is text or"),
        ({"a": {"base64": "!!"}}, "does not decode"),
        ({"a": "x" * 10}, "files are over"),
    ],
)
def test_bad_files_are_refused(monkeypatch, files, why):
    monkeypatch.setattr(clis, "FILES_MAX", 5)
    with pytest.raises(BadRequest, match=why):
        clis.check_files(files)


def test_the_files_the_command_made_or_changed_come_back_capped(tmp_path, monkeypatch):
    given = clis.write_files(tmp_path, {"same.txt": b"same", "changed.txt": b"before", "d/in.txt": b"x"})
    (tmp_path / "changed.txt").write_text("after")
    (tmp_path / "out.bin").write_bytes(b"\xff\xfe")
    (tmp_path / "link").symlink_to(tmp_path / "same.txt")
    (tmp_path / "z-big.txt").write_text("y" * 10)
    monkeypatch.setattr(clis, "FILES_MAX", 12)
    made, skipped = clis.collect(tmp_path, given)
    assert made == {"changed.txt": "after", "out.bin": {"base64": base64.b64encode(b"\xff\xfe").decode()}}
    assert skipped == ["z-big.txt"]
    monkeypatch.setattr(clis, "FILES_COUNT", 1)
    assert clis.collect(tmp_path, given)[1] == ["out.bin", "z-big.txt"]
