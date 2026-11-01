"""CLIs: command-line tools rrotor installs at deploy and runs in the sandbox (docs/architecture.md, 5.7).

**Install**, once per version, under its folder (`/data/programs/<name>/versions/<sha>/cli/`), pinned: the download's
SHA-256 is checked before anything is unpacked, and no code of the CLI runs outside the sandbox (no npm or setup
scripts). From a release `url` (a tarball, a zip or a bare binary), from `npm` (the package's tarball from its
registry; a package must carry its dependencies, `bundleDependencies`), from `pypi` (the wheel with that SHA-256,
found through PyPI's JSON API and unpacked by rrotor; its dependencies from the commit's `requirements.lock`, hashed,
binary wheels only, as a program's) or `image` (a binary already in the image, under /usr). Downloads go over HTTPS
to the manifest's `hosts` and the hosts the source implies (`Install.implied_hosts`), redirects included, and only to
public addresses, as the egress proxy lets a run's connections through.

**Run**: the arguments are checked against `allow` (globs over the arguments as a shell would quote them, `shlex`)
and `approvals`; the inputs (stdin, files for the per-call working folder) are capped; files the command made or
changed come back, capped too."""

from __future__ import annotations

import base64
import binascii
import configparser
import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from rrotor.http import host_allowed
from rrotord.errors import BadRequest, DeployError
from rrotord.manifest import NPM, NPM_REGISTRY, PYPI, Manifest
from rrotord.proc import run
from rrotord.proxy import public

INSTALLED = "installed.json"
DOWNLOAD_MAX = 512 * 1024 * 1024
UNPACKED_MAX = 1024 * 1024 * 1024
REDIRECTS = 5
IMAGE_PATH = "/usr/local/bin:/usr/bin:/bin"
IMAGE_ROOTS = ("/usr/", "/bin/")  # what the sandbox mounts (/bin is /usr/bin there)
TARBALLS = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar")
ARGS_MAX, ARG_MAX = 256, 8192
STDIN_MAX = 1024 * 1024
FILES_MAX, FILES_COUNT = 1024 * 1024, 64  # in and out, each way
OUTPUT_MAX = 64 * 1024  # stdout and stderr, each, in the Result (the rest in the run's log)
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
SCRIPT = """import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site"))
target = importlib.import_module({module!r})
for name in {attrs!r}:
    target = getattr(target, name)
sys.exit(target())
"""


def refused(message: str, code: str = "E_CLI_INSTALL") -> DeployError:
    return DeployError(message, code=code)


# Whose HOME a CLI's run gets (docs/architecture.md, 5.7, `home`): a Glyphh sign-in's subject has their own,
# `homes/<subject>`, so each person on a rrotor is signed in as themselves; everything else (a runtime key, the local
# token, the link, a schedule, rrotor itself) shares the CLI's own, `home`. A subject plain enough to be a folder name
# is used as is; any other is hashed.
SUBJECT_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}")


def subject_of(actor: dict[str, Any]) -> str | None:
    """The signed-in person a run is for, from the caller's actor (rrotord/audit.py): a `sign-in`'s `sub`, else
    None (the CLI's own HOME)."""
    sub = actor.get("sub") if actor.get("type") == "sign-in" else None
    return sub if isinstance(sub, str) and sub else None


def home_path(folder: Path, subject: str | None) -> Path:
    """Where a CLI's HOME is on the volume, under the program's folder: `home`, the CLI's own, or `homes/<subject>`,
    a signed-in person's own (their sign-in and config, nobody else's)."""
    if subject is None:
        return folder / "home"
    safe = subject if SUBJECT_PLAIN.fullmatch(subject) else hashlib.sha256(subject.encode()).hexdigest()[:32]
    return folder / "homes" / safe


# Fetching


class Fetch(Protocol):
    async def __call__(self, url: str, hosts: frozenset[str]) -> bytes:
        """The body at `url`, over HTTPS, from `hosts` only (redirects too); DeployError otherwise."""
        ...


def check_url(url: str, hosts: frozenset[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.port not in (None, 443):
        raise refused(f"{url} is not https on 443: an install downloads over HTTPS only.")
    if not host_allowed(parts.hostname or "", hosts):
        raise refused(f"{parts.hostname} is not in the CLI's hosts or the ones its install implies.")


class PublicResolver(AbstractResolver):
    """A host's public addresses only: an install never reaches a loopback, private or link-local one."""

    def __init__(self, inner: AbstractResolver | None = None) -> None:
        self.inner = inner or aiohttp.ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        found = [r for r in await self.inner.resolve(host, port, family) if public(r["host"])]
        if not found:
            raise OSError(f"{host} resolves only to private or local addresses")
        return found

    async def close(self) -> None:
        await self.inner.close()


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver()), timeout=aiohttp.ClientTimeout(total=600)
    )


class HttpsFetch:
    """The install's downloads: HTTPS, the hosts allowed at every redirect, public addresses, 512 MB at most."""

    def __init__(self, session: Callable[[], Any] = _session) -> None:
        self.session = session

    async def __call__(self, url: str, hosts: frozenset[str]) -> bytes:
        try:
            async with self.session() as session:
                return await self._get(session, url, hosts)
        except (aiohttp.ClientError, OSError, TimeoutError) as err:
            raise refused(f"{urlsplit(url).hostname} could not be downloaded from: {err}") from None

    async def _get(self, session: Any, url: str, hosts: frozenset[str]) -> bytes:
        for _ in range(REDIRECTS + 1):
            check_url(url, hosts)
            async with session.get(url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                    url = urljoin(url, response.headers["Location"])
                    continue
                if response.status != 200:
                    raise refused(f"{url} answered {response.status}.")
                body = bytearray()
                async for chunk in response.content.iter_chunked(1 << 16):
                    body += chunk
                    if len(body) > DOWNLOAD_MAX:
                        raise refused(f"{url} is over {DOWNLOAD_MAX >> 20} MB.")
                return bytes(body)
        raise refused(f"{url} redirects more than {REDIRECTS} times.")


# Installing


@dataclass(frozen=True, slots=True)
class Installed:
    """What a version's install runs: an executable, a Node script or a Python console script, by its path in the
    install folder (or in the image, `absolute`)."""

    run: Literal["exec", "node", "python"]
    path: str
    absolute: bool = False

    def where(self, root: str) -> str:
        """Its path, with the install folder at `root` (inside the sandbox, /cli)."""
        return self.path if self.absolute else f"{root}/{self.path}"


def installed(folder: Path) -> Installed | None:
    """A version's install, from its installed.json; None when there is none (or it does not read)."""
    try:
        data = json.loads((folder / INSTALLED).read_text())
        return Installed(data["run"], str(data["path"]), bool(data.get("absolute")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def verify(data: bytes, sha256: str | None, what: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if sha256 is not None and actual != sha256:
        raise refused(f"{what}'s SHA-256 is {actual}, not the {sha256} glyphh.json pins.", "E_CLI_CHECKSUM")


def _inside(root: Path, rel: str) -> Path:
    dest = (root / rel).resolve()
    if not dest.is_relative_to(root.resolve()):
        raise refused(f"The archive has {rel}, outside its folder.")
    return dest


def _untar(data: bytes, root: Path) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            if sum(m.size for m in tar.getmembers()) > UNPACKED_MAX:
                raise refused(f"The archive unpacks to over {UNPACKED_MAX >> 30} GB.")
            tar.extractall(root, filter="data")
    except tarfile.TarError as err:
        raise refused(f"The archive does not unpack: {err}") from None


def _unzip(data: bytes, root: Path, members: Callable[[str], str | None] = lambda name: name) -> dict[str, bytes]:
    """The zip's files under root, each where `members` puts it (None: left out), modes kept for executables.
    Answers the contents of the files kept, for the caller to read what it needs."""
    kept: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > UNPACKED_MAX:
                raise refused(f"The archive unpacks to over {UNPACKED_MAX >> 30} GB.")
            for info in archive.infolist():
                rel = members(info.filename)
                if info.is_dir() or rel is None:
                    continue
                dest = _inside(root, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                content = archive.read(info)
                dest.write_bytes(content)
                if (info.external_attr >> 16) & 0o111:
                    dest.chmod(0o755)
                kept[info.filename] = content
    except zipfile.BadZipFile as err:
        raise refused(f"The archive does not unpack: {err}") from None
    return kept


def _find(root: Path, command: str) -> Path:
    """The command in the install: its path, or the file of that name nearest the top."""
    named = [root / command] if "/" in command else sorted(root.rglob(command), key=lambda p: (len(p.parts), str(p)))
    for path in named:
        if path.is_file() and path.resolve().is_relative_to(root.resolve()):
            return path
    raise refused(f"The install has no {command}: name the command's path inside it.")


def _executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IXGRP | stat.S_IXOTH)


def _wheel_member(name: str) -> str | None:
    """Where a wheel's file goes in site/: its .data's purelib and platlib beside the rest; its scripts, headers and
    data files are left out."""
    first, _, rest = name.partition("/")
    if not first.endswith(".data"):
        return name
    kind, _, inner = rest.partition("/")
    return inner if kind in ("purelib", "platlib") and inner else None


def console_script(entry_points: str, command: str) -> tuple[str, tuple[str, ...]]:
    """A console script's module and attribute path, from the wheel's entry_points.txt."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string(entry_points)
        spec = parser.get("console_scripts", command)
    except (configparser.Error, KeyError) as err:
        names = parser.options("console_scripts") if parser.has_section("console_scripts") else []
        raise refused(f"The wheel has no console script {command} (it has: {', '.join(names) or 'none'}).") from err
    module, _, attrs = spec.split("[", 1)[0].strip().partition(":")
    if not IDENTIFIER.match(module) or not IDENTIFIER.match(attrs):
        raise refused(f"The wheel's console script {command} is {spec!r}, not module:function.")
    return module, tuple(attrs.split("."))


class Installer:
    """A version's CLI, installed into its folder at deploy."""

    def __init__(
        self,
        fetch: Fetch | None = None,
        *,
        uv: str = "uv",
        python: str = "/usr/bin/python3.13",
        which: Callable[..., str | None] = shutil.which,
    ) -> None:
        self.fetch: Fetch = fetch or HttpsFetch()
        self.uv, self.python, self.which = uv, python, which

    async def install(self, manifest: Manifest, target: Path, src: Path) -> Installed:
        """The CLI `manifest` names, into `target` (made here), its installed.json written last."""
        cli = manifest.cli
        assert cli is not None  # noqa: S101  (a manifest of kind cli has one)
        hosts = frozenset(manifest.hosts) | cli.install.implied_hosts()
        target.mkdir(parents=True)
        match cli.install.source:
            case "url":
                made = await self._url(str(cli.install.url), cli.install.sha256, cli.command, hosts, target)
            case "npm":
                made = await self._npm(str(cli.install.npm), cli.install.sha256, cli.command, hosts, target)
            case "pypi":
                made = await self._pypi(str(cli.install.pypi), cli.install.sha256, cli.command, hosts, target, src)
            case _:
                made = self._image(str(cli.install.image), cli.install.sha256)
        (target / INSTALLED).write_text(json.dumps(asdict(made)))
        return made

    async def _download(self, url: str, sha256: str | None, hosts: frozenset[str]) -> bytes:
        data = await self.fetch(url, hosts)
        verify(data, sha256, url)
        return data

    async def _url(self, url: str, sha256: str | None, command: str, hosts: frozenset[str], target: Path) -> Installed:
        data = await self._download(url, sha256, hosts)
        name, files = PurePosixPath(urlsplit(url).path).name.lower(), target / "files"
        files.mkdir()
        if name.endswith(TARBALLS):
            _untar(data, files)
        elif name.endswith(".zip"):
            _unzip(data, files)
        else:  # a bare binary: the command itself
            (files / command).parent.mkdir(parents=True, exist_ok=True)
            (files / command).write_bytes(data)
        found = _find(files, command)
        _executable(found)
        return Installed("exec", found.relative_to(target).as_posix())

    async def _npm(self, spec: str, sha256: str | None, command: str, hosts: frozenset[str], target: Path) -> Installed:
        m = NPM.match(spec)
        assert m is not None  # noqa: S101  (the manifest checked it)
        name, version = m["name"], m["version"]
        url = f"https://{NPM_REGISTRY}/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"
        files = target / "files"
        files.mkdir()
        _untar(await self._download(url, sha256, hosts), files)
        try:
            package = json.loads((files / "package" / "package.json").read_text())
        except (OSError, ValueError) as err:
            raise refused(f"{spec} has no package/package.json that reads: {err}") from None
        bundled = set(package.get("bundleDependencies") or package.get("bundledDependencies") or [])
        missing = sorted(set(package.get("dependencies") or {}) - bundled)
        if missing:
            raise refused(
                f"{spec} depends on {', '.join(missing)}, not bundled in it: rrotor installs a package that carries "
                "its dependencies (bundleDependencies), or one with none."
            )
        bins = package.get("bin")
        if isinstance(bins, str):
            bins = {name.rsplit("/", 1)[-1]: bins}
        script = bins.get(command) if isinstance(bins, dict) else None
        if not isinstance(script, str):
            have = ", ".join(sorted(bins)) if isinstance(bins, dict) else "none"
            raise refused(f"{spec} has no bin {command} (it has: {have}).")
        found = _find(files / "package", str(PurePosixPath(script)).removeprefix("./"))
        return Installed("node", found.relative_to(target).as_posix())

    async def _pypi(
        self, spec: str, sha256: str | None, command: str, hosts: frozenset[str], target: Path, src: Path
    ) -> Installed:
        m = PYPI.match(spec)
        assert m is not None  # noqa: S101  (the manifest checked it)
        name, version = m["name"], m["version"]
        listing = await self.fetch(f"https://pypi.org/pypi/{name}/{version}/json", hosts)
        try:
            urls = json.loads(listing)["urls"]
            wheel = next(
                f["url"]
                for f in urls
                if f.get("packagetype") == "bdist_wheel" and (f.get("digests") or {}).get("sha256") == sha256
            )
        except (ValueError, KeyError, TypeError, StopIteration):
            raise refused(f"PyPI has no wheel of {spec} whose SHA-256 is {sha256}.", "E_CLI_CHECKSUM") from None
        site = target / "site"
        site.mkdir()
        kept = _unzip(await self._download(str(wheel), sha256, hosts), site, _wheel_member)
        entry_points = next(
            (v.decode(errors="replace") for k, v in kept.items() if k.endswith(".dist-info/entry_points.txt")), ""
        )
        module, attrs = console_script(entry_points, command)
        lock = src / "requirements.lock"
        if lock.is_file():
            await self._dependencies(lock, site, target)
        script = target / "bin" / command
        script.parent.mkdir()
        script.write_text(SCRIPT.format(module=module, attrs=attrs))
        return Installed("python", f"bin/{command}")

    async def _dependencies(self, lock: Path, site: Path, home: Path) -> None:
        """The commit's requirements.lock into site/: hashed, binary wheels only, no dependency of its own."""
        argv = [self.uv, "pip", "install", "--target", str(site), "--python", self.python]
        argv += ["--require-hashes", "--only-binary", ":all:", "--no-deps", "-r", str(lock)]
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "UV_NO_CACHE": "1"}
        done = await run(argv, env=env, timeout=600)
        if done.code != 0:
            raise refused(f"requirements.lock could not be installed: {done.why()}", "E_DEPS")

    def _image(self, image: str, sha256: str | None) -> Installed:
        path = image if image.startswith("/") else self.which(image, path=IMAGE_PATH)
        if path is None or not Path(path).is_file():
            raise refused(f"The image has no {image}.")
        if not path.startswith(IMAGE_ROOTS):
            raise refused(f"{image} is at {path}, outside /usr: the sandbox does not have it.")
        if sha256 is not None:  # read only to check it (a vendor's binary is hundreds of MB)
            verify(Path(path).read_bytes(), sha256, path)
        return Installed("exec", path, absolute=True)


# Running


def command_line(args: list[str]) -> str:
    """The arguments as one line, quoted as a shell would: what `allow` and `approvals` match."""
    return shlex.join(args)


def matching(patterns: list[str], args: list[str]) -> str | None:
    """The first pattern the arguments match, if any."""
    line = command_line(args)
    return next((p for p in patterns if fnmatch.fnmatchcase(line, p)), None)


def allowed(patterns: list[str], args: list[str]) -> bool:
    return not patterns or matching(patterns, args) is not None


def check_args(args: Any) -> list[str]:
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise BadRequest("args is a list of strings.", code="E_CLI_INPUTS")
    if len(args) > ARGS_MAX or any(len(a) > ARG_MAX or "\x00" in a for a in args):
        raise BadRequest(f"At most {ARGS_MAX} args, each up to {ARG_MAX} characters, no NUL.", code="E_CLI_INPUTS")
    return list(args)


def check_stdin(stdin: Any) -> str | None:
    if stdin is not None and (not isinstance(stdin, str) or len(stdin.encode()) > STDIN_MAX):
        raise BadRequest(f"stdin is text, up to {STDIN_MAX >> 20} MB.", code="E_CLI_INPUTS")
    return stdin


def check_files(files: Any) -> dict[str, bytes]:
    """The working folder's files, by relative path: text, or {"base64": ...}; 64 files and 1 MB at most."""
    if files is None:
        return {}
    if not isinstance(files, dict) or len(files) > FILES_COUNT:
        raise BadRequest(f"files is an object: up to {FILES_COUNT} paths, each its content.", code="E_CLI_INPUTS")
    out: dict[str, bytes] = {}
    for name, value in files.items():
        parts = PurePosixPath(name).parts
        if not name or name.startswith("/") or {".", ".."} & set(parts) or "\x00" in name or len(name) > 255:
            raise BadRequest(f"files: {name!r} is not a relative path inside the folder.", code="E_CLI_INPUTS")
        if isinstance(value, str):
            out[PurePosixPath(name).as_posix()] = value.encode()
        elif isinstance(value, dict) and isinstance(value.get("base64"), str):
            try:
                out[PurePosixPath(name).as_posix()] = base64.b64decode(value["base64"], validate=True)
            except (binascii.Error, ValueError):
                raise BadRequest(f"files: {name}'s base64 does not decode.", code="E_CLI_INPUTS") from None
        else:
            raise BadRequest(f'files: {name} is text or {{"base64": ...}}.', code="E_CLI_INPUTS")
    if sum(len(v) for v in out.values()) > FILES_MAX:
        raise BadRequest(f"files are over {FILES_MAX >> 20} MB.", code="E_CLI_INPUTS")
    return out


def write_files(work: Path, files: dict[str, bytes]) -> dict[str, str]:
    """The files into the working folder; answers each one's SHA-256 (to know, after, which the command changed)."""
    for name, content in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_bytes(content)
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


def collect(work: Path, given: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """The files the command made or changed in its working folder: text, or {"base64": ...} when not UTF-8; past
    64 files or 1 MB, the rest are only named (`skipped`)."""
    out: dict[str, Any] = {}
    skipped: list[str] = []
    size = 0
    for path in sorted(p for p in work.rglob("*") if p.is_file() and not p.is_symlink()):
        name = path.relative_to(work).as_posix()
        content = path.read_bytes()
        if given.get(name) == hashlib.sha256(content).hexdigest():
            continue
        if len(out) >= FILES_COUNT or size + len(content) > FILES_MAX:
            skipped.append(name)
            continue
        size += len(content)
        try:
            out[name] = content.decode()
        except UnicodeDecodeError:
            out[name] = {"base64": base64.b64encode(content).decode()}
    return out, skipped
