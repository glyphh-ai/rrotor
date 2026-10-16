"""glyphh.json, the program's manifest (docs/architecture.md, 5.2): read, checked, and its functions found in
program.py without running it (its syntax tree), since no program code runs outside the sandbox."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rrotord.errors import BadRequest, DeployError

NAME = r"^[a-z][a-z0-9_]{0,62}$"
FUNCTION = r"^[a-z][a-z0-9_]{0,62}$"
HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PYTHON = "3.13"
RESERVED = frozenset({"load", "end"})


class Function(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    inputs: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    approvals: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]] = []

    @field_validator("inputs")
    @classmethod
    def _schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            jsonschema.Draft202012Validator.check_schema(value)
        except jsonschema.SchemaError as err:
            raise ValueError(f"inputs is not a JSON Schema: {err.message}") from None
        return value


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: int = Field(300, ge=1, le=3600)
    memory_mb: int = Field(512, ge=64, le=8192)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(pattern=NAME)]
    version: int | None = Field(None, ge=1)
    description: str = ""
    python: str = PYTHON
    functions: dict[Annotated[str, Field(pattern=FUNCTION)], Function]
    hosts: list[str] = []
    secrets: list[Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]] = []
    databases: list[Literal["postgres", "mongodb", "neo4j"]] = []
    limits: Limits = Limits()
    connectors: list[str] = []
    data: bool = False

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
        if not self.functions:
            raise ValueError("functions names at least one function")
        reserved = RESERVED & set(self.functions)
        if reserved:
            raise ValueError(f"{', '.join(sorted(reserved))} cannot be a function: load and end run on their own")
        return self

    def parts(self) -> list[str]:
        """The parts this program needs beside the base (docs/architecture.md, 10)."""
        return list(self.databases)


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
    if not program.is_file():
        raise DeployError("The commit has no program.py.", code="E_MANIFEST")
    try:
        manifest = Manifest.model_validate(json.loads(path.read_text()))
    except (ValueError, ValidationError) as err:
        errors = err.errors(include_url=False, include_input=False) if isinstance(err, ValidationError) else None
        why = (
            "; ".join(f"{'.'.join(map(str, e['loc'])) or 'glyphh.json'}: {e['msg']}" for e in errors)
            if errors
            else str(err)
        )
        raise DeployError(f"glyphh.json is refused: {why}", code="E_MANIFEST") from None
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
