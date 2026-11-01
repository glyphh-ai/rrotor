"""glyphh.json, the program's manifest (docs/architecture.md, 5.2): read, checked, and its functions found in
program.py without running it (its syntax tree), since no program code runs outside the sandbox."""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rrotord.cron import Cron, zone
from rrotord.errors import BadRequest, DeployError

NAME = r"^[a-z][a-z0-9_]{0,62}$"
FUNCTION = r"^[a-z][a-z0-9_]{0,62}$"
CAPS = r"^[A-Z][A-Z0-9_]{0,63}$"  # a secret's name
HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PYTHON = "3.13"
RESERVED = frozenset({"load", "end"})
NPM = re.compile(
    r"^(?P<name>(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*)@"
    r"(?P<version>\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?)$"
)
PYPI = re.compile(r"^(?P<name>[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)==(?P<version>[0-9][0-9A-Za-z.!+_-]*)$")
IMAGE = re.compile(r"^(/usr/[A-Za-z0-9._+/-]+|[A-Za-z0-9._+-]{1,64})$")
COMMAND = re.compile(r"^[A-Za-z0-9._+-]{1,128}(/[A-Za-z0-9._+-]{1,128}){0,15}$")
ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
# secret:NAME takes a secret the run needs; secret?:NAME one it passes only when it is set (an optional API key)
SECRET_REF = re.compile(r"^secret(?P<optional>\?)?:(?P<name>[A-Z][A-Z0-9_]{0,63})$")
HOME_FILE = re.compile(r"^[A-Za-z0-9._-]{1,128}(/[A-Za-z0-9._-]{1,128}){0,7}$")
# What rrotor sets in a CLI's environment itself: its PATH, HOME and temporary folder, and the egress proxy
ENV_RESERVED = frozenset({"PATH", "HOME", "TMPDIR", "NODE_USE_ENV_PROXY"})
PROXY_ENV = re.compile(r"^(http|https|all|no)_proxy$", re.IGNORECASE)
# The hosts an install source reaches beside the manifest's own (docs/architecture.md, 5.7): a release URL's host
# (GitHub serves its release assets from its object storage), npm's registry, PyPI's API and its files
GITHUB_ASSETS = frozenset({"objects.githubusercontent.com", "release-assets.githubusercontent.com"})
NPM_REGISTRY = "registry.npmjs.org"
PYPI_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})


class Function(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    inputs: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    approvals: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]] = []
    writes: bool | None = None  # whether it runs on the head; unset: the program's `data` (docs/specs/cluster.md, 3)

    @field_validator("inputs")
    @classmethod
    def _schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            jsonschema.Draft202012Validator.check_schema(value)
        except jsonschema.SchemaError as err:
            raise ValueError(f"inputs is not a JSON Schema: {err.message}") from None
        return value


class Schedule(BaseModel):
    """A function the daemon calls on a cron schedule (docs/architecture.md, 5.6)."""

    model_config = ConfigDict(extra="forbid")
    function: Annotated[str, Field(pattern=FUNCTION)]
    cron: str
    tz: str = "UTC"
    input: dict[str, Any] = Field(default_factory=dict)
    name: Annotated[str, Field(pattern=NAME)] | None = None  # the function's name by default

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        Cron.parse(value)
        return value

    @field_validator("tz")
    @classmethod
    def _tz(cls, value: str) -> str:
        zone(value)
        return value

    @property
    def id(self) -> str:
        return self.name or self.function

    def next_after(self, after: dt.datetime) -> dt.datetime:
        return Cron.parse(self.cron).next_after(after, zone(self.tz))


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: int = Field(300, ge=1, le=3600)
    memory_mb: int = Field(512, ge=64, le=8192)


SOURCES = ("url", "npm", "pypi", "image")


class Install(BaseModel):
    """Where a CLI comes from, pinned (docs/architecture.md, 5.7): one source, and the SHA-256 of what is downloaded
    (optional for a binary already in the image, checked when given)."""

    model_config = ConfigDict(extra="forbid")
    url: str | None = None  # a release's tarball, zip or binary, over HTTPS
    npm: str | None = None  # name@exact-version
    pypi: str | None = None  # name==exact-version: the wheel whose SHA-256 is sha256
    image: str | None = None  # a binary in the image: its name on PATH or its path under /usr
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def _source(self) -> Install:
        given = [k for k in SOURCES if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError("install names one source: url, npm, pypi or image")
        if self.sha256 is None and self.source != "image":
            raise ValueError(f"install from {self.source} needs its sha256, the checksum of what is downloaded")
        if self.url is not None:
            parts = urlsplit(self.url)
            if parts.scheme != "https" or not HOST.match(parts.hostname or "") or parts.username or parts.port:
                raise ValueError("install.url is an https:// address on port 443, with no user or password")
        if self.npm is not None and not NPM.match(self.npm):
            raise ValueError("install.npm is name@version, an exact version (1.2.3)")
        if self.pypi is not None and not PYPI.match(self.pypi):
            raise ValueError("install.pypi is name==version, an exact version")
        if self.image is not None and (not IMAGE.match(self.image) or ".." in self.image.split("/")):
            raise ValueError("install.image is a binary's name on PATH, or its path under /usr")
        return self

    @property
    def source(self) -> str:
        return next(k for k in SOURCES if getattr(self, k) is not None)

    @property
    def pinned(self) -> str:
        """What is installed, as the manifest names it."""
        return str(getattr(self, self.source))

    def implied_hosts(self) -> frozenset[str]:
        """The hosts the install reaches beside the manifest's own `hosts`."""
        if self.url is not None:
            host = urlsplit(self.url).hostname or ""
            return frozenset({host} | (GITHUB_ASSETS if host == "github.com" else set()))
        if self.npm is not None:
            return frozenset({NPM_REGISTRY})
        if self.pypi is not None:
            return PYPI_HOSTS
        return frozenset()


Pattern = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00\n\r]+$")]
Env = dict[Annotated[str, Field(pattern=ENV_NAME)], Annotated[str, Field(max_length=4096)]]


def _check_env(value: dict[str, str]) -> dict[str, str]:
    for name, text in value.items():
        if name in ENV_RESERVED or name.startswith("LD_") or PROXY_ENV.match(name):
            raise ValueError(f"{name} is set by rrotor (PATH, HOME, TMPDIR, the proxy, LD_*): leave it out")
        if text.startswith(("secret:", "secret?:")) and not SECRET_REF.match(text):
            raise ValueError(f"{name}: secret:NAME (or secret?:NAME) names a secret, ^[A-Z][A-Z0-9_]{{0,63}}$")
    return value


def _refs(env: dict[str, str]) -> dict[str, re.Match[str]]:
    return {k: m for k, v in env.items() if (m := SECRET_REF.match(v))}


class Login(BaseModel):
    """How a CLI signs in from afar (docs/architecture.md, 5.7, signing in): the command that starts its sign-in, run
    on a pseudo-terminal and kept running while the person opens its address on their phone; the code they paste (or
    the address the browser ended on) is typed into it by program_continue."""

    model_config = ConfigDict(extra="forbid")
    args: list[Annotated[str, Field(max_length=8192)]] = Field(max_length=16)  # its arguments ([]: the bare command)
    env: Env = {}  # beside the CLI's own env, for the sign-in only
    listen: bool = False  # it listens on loopback for the browser's redirect: TCP inside the sandbox left open
    paste: bool = True  # it waits for a code pasted back; false: it finds out itself (a device code, polling)
    code: Annotated[str, Field(max_length=200)] | None = None  # a regex for its device code; a generic one otherwise
    signed_in: Annotated[str, Field(pattern=HOME_FILE.pattern)] | None = None  # a file in HOME that means it is done
    home_files: dict[Annotated[str, Field(pattern=HOME_FILE.pattern)], Annotated[str, Field(max_length=65536)]] = {}
    seconds: int = Field(600, ge=30, le=1800)  # its time running (not the time it waits for the person)

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_env(value)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as err:
                raise ValueError(f"login.code is not a regular expression: {err}") from None
        return value

    @field_validator("signed_in", "home_files")
    @classmethod
    def _inside(cls, value: Any) -> Any:
        for path in [value] if isinstance(value, str) else (value or {}):
            if {".", ".."} & set(path.split("/")):
                raise ValueError(f"{path} is a path inside HOME, without . or ..")
        return value


class Cli(BaseModel):
    """A command-line tool rrotor runs in the sandbox (`"kind": "cli"`, docs/architecture.md, 5.7)."""

    model_config = ConfigDict(extra="forbid")
    install: Install
    command: str  # what runs: a path in the install (or a name found in it), npm's bin, PyPI's console script
    allow: list[Pattern] = []  # globs over the arguments, joined as a shell would quote them; none: any
    approvals: list[Pattern] = []  # the arguments that wait for the person's approval first
    env: Env = {}
    home: bool = False  # a private HOME kept across calls
    interactive: bool = False
    login: Login | None = None  # how it signs in from afar (cli_login); needs its private HOME to keep the sign-in

    @field_validator("command")
    @classmethod
    def _command(cls, value: str) -> str:
        if not COMMAND.match(value) or {".", ".."} & set(value.split("/")):
            raise ValueError("command is a name, or a relative path inside the install")
        return value

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_env(value)

    @field_validator("interactive")
    @classmethod
    def _interactive(cls, value: bool) -> bool:
        if value:
            raise ValueError("interactive sessions are not built yet: this release runs one-shot commands only")
        return value

    @model_validator(mode="after")
    def _image(self) -> Cli:
        image = self.install.image
        if image is not None and self.command != image.rsplit("/", 1)[-1]:
            raise ValueError(f"command is the image binary's name, {image.rsplit('/', 1)[-1]}")
        if self.login is not None and not self.home:
            raise ValueError("login keeps its sign-in in the CLI's private HOME: set home to true")
        return self

    def secret_refs(self, login: bool = False) -> dict[str, str]:
        """Each variable that takes a secret, and the secret's name (a sign-in's own env beside, with `login`)."""
        env = self.env | (self.login.env if login and self.login else {})
        return {k: m["name"] for k, m in _refs(env).items()}

    def optional_secrets(self) -> set[str]:
        """The secrets only ever taken as secret?:NAME: a run goes without them when they are not set."""
        refs = list(_refs(self.env).values()) + (list(_refs(self.login.env).values()) if self.login else [])
        return {m["name"] for m in refs} - {m["name"] for m in refs if not m["optional"]}


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(pattern=NAME)]
    kind: Literal["program", "cli"] = "program"  # what runs: program.py's functions, or a CLI's command
    version: int | None = Field(None, ge=1)
    description: str = ""
    python: str = PYTHON
    functions: dict[Annotated[str, Field(pattern=FUNCTION)], Function] = {}  # a program's: at least one
    cli: Cli | None = None  # a CLI's: what it installs and runs
    hosts: list[str] = []
    secrets: list[Annotated[str, Field(pattern=CAPS)]] = []  # what it reads from the vault (rrotord/vault.py)
    # sqlite: a database file in its data folder, opened as ctx.db (rrotor's own); the others are parts, not built yet
    databases: list[Literal["sqlite", "postgres", "mongodb", "neo4j"]] = []
    needs: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")]] = []  # parts by name
    limits: Limits = Limits()
    connectors: list[str] = []
    data: bool = False
    schedules: list[Schedule] = []  # functions the daemon calls on a cron schedule (rrotord/scheduler.py)
    clis: list[Annotated[str, Field(pattern=NAME)]] = []  # the CLIs on this rrotor its functions run (ctx.cli)

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        for host in value:
            if not HOST.match(host):
                raise ValueError(f"{host!r} is not a host name")
        return value

    @field_validator("python")
    @classmethod
    def _python(cls, value: str) -> str:
        if value != PYTHON:
            raise ValueError(f"this rrotor runs Python {PYTHON}")
        return value

    @model_validator(mode="after")
    def _functions(self) -> Manifest:
        if self.kind == "cli":
            return self._a_cli()
        if self.cli is not None:
            raise ValueError('cli is for a manifest of "kind": "cli"')
        if not self.functions:
            raise ValueError("functions names at least one function")
        reserved = RESERVED & set(self.functions)
        if reserved:
            raise ValueError(f"{', '.join(sorted(reserved))} cannot be a function: load and end run on their own")
        for function in self.functions.values():
            if function.writes is None:
                function.writes = self.keeps_data  # safe: a program with data writes unless it says one does not
        return self

    def _a_cli(self) -> Manifest:
        if self.cli is None:
            raise ValueError('a manifest of "kind": "cli" has a cli block: install, command, allow, approvals, env')
        given = [
            key
            for key, value in (
                ("functions", self.functions),
                ("schedules", self.schedules),
                ("data", self.data),
                ("databases", self.databases),
                ("needs", self.needs),
                ("clis", self.clis),
            )
            if value
        ]
        if given:
            raise ValueError(f"a cli runs its command, so it has no {', '.join(given)}")
        unset = sorted(set(self.cli.secret_refs(login=True).values()) - set(self.secrets))
        if unset:
            raise ValueError(f"cli.env takes the secret {', '.join(unset)}, not in secrets: declare it there")
        return self

    @model_validator(mode="after")
    def _schedules(self) -> Manifest:
        seen: set[str] = set()
        for i, schedule in enumerate(self.schedules):
            where = f"schedules.{i}"
            function = self.functions.get(schedule.function)
            if function is None:
                raise ValueError(f"{where}: {schedule.function} is not one of its functions")
            errors = sorted(jsonschema.Draft202012Validator(function.inputs).iter_errors(schedule.input), key=str)
            if errors:
                raise ValueError(f"{where}.input does not fit {schedule.function}'s inputs: {errors[0].message}")
            if schedule.id in seen:
                raise ValueError(f"{where}: two schedules are named {schedule.id}; give each its own name")
            seen.add(schedule.id)
        return self

    @property
    def keeps_data(self) -> bool:
        """Whether its persistent data folder is mounted: it asks for one (`data`), or for a SQLite database, a file
        in it (ctx.db)."""
        return self.data or "sqlite" in self.databases

    @property
    def head_only(self) -> bool:
        """A program that keeps state in its data folder (its SQLite database too), which never replicates, or reads
        secrets, which only the head's vault holds, or a CLI (its install, the head's build, and its home never
        replicate), or a program that runs CLIs (which run on the head): every call to it runs on the head."""
        return self.keeps_data or bool(self.secrets) or self.kind == "cli" or bool(self.clis)

    def writes(self, function: str) -> bool:
        """Whether a call of `function` runs on the cluster's head: it writes, or its program is the head's alone. A
        function the manifest does not name writes (the head says it is missing)."""
        found = self.functions.get(function)
        return found is None or bool(found.writes) or self.head_only

    def parts(self) -> list[str]:
        """The parts this program needs beside the base (docs/architecture.md, 10): its databases (but SQLite, which
        is rrotor's own), and the ones it names, each once, in order."""
        return list(dict.fromkeys([*(d for d in self.databases if d != "sqlite"), *self.needs]))


def _defined(program: Path) -> set[str]:
    try:
        tree = ast.parse(program.read_bytes(), filename="program.py")
    except SyntaxError as err:
        raise DeployError(f"program.py does not parse: line {err.lineno}: {err.msg}", code="E_MANIFEST") from None
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def read(src: Path) -> Manifest:
    """The manifest of a checked-out version, refused with E_MANIFEST and why."""
    path, program = src / "glyphh.json", src / "program.py"
    if not path.is_file():
        raise DeployError("The commit has no glyphh.json.", code="E_MANIFEST")
    try:
        raw = json.loads(path.read_text())
        cli = isinstance(raw, dict) and raw.get("kind") == "cli"  # a CLI needs no program.py
        if not cli and not program.is_file():
            raise DeployError("The commit has no program.py.", code="E_MANIFEST")
        manifest = Manifest.model_validate(raw)
    except (ValueError, ValidationError) as err:
        errors = err.errors(include_url=False, include_input=False) if isinstance(err, ValidationError) else None
        why = (
            "; ".join(f"{'.'.join(map(str, e['loc'])) or 'glyphh.json'}: {e['msg']}" for e in errors)
            if errors
            else str(err)
        )
        raise DeployError(f"glyphh.json is refused: {why}", code="E_MANIFEST") from None
    if manifest.kind == "cli":
        return manifest
    missing = sorted(set(manifest.functions) - _defined(program))
    if missing:
        raise DeployError(f"program.py has no function {', '.join(missing)}.", code="E_MANIFEST")
    return manifest


def check_inputs(function: Function, inputs: dict[str, Any]) -> None:
    errors = sorted(jsonschema.Draft202012Validator(function.inputs).iter_errors(inputs), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(map(str, e.path)) or "inputs"
        raise BadRequest(f"{where}: {e.message}", code="E_INPUTS")
