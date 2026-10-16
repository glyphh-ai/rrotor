"""Parts: what a mod installs into rrotor beyond the base (docs/architecture.md, 10). A part is a tarball built in the
mod's own CI, signed with Glyphh's release key and named with its SHA-256; rrotor checks both before it unpacks
anything, keeps each version under /data/parts/<name>/<version>/ and switches /data/parts/<name>/current in one
rename, so a failed install leaves the one before. A run mounts the parts its manifest asks for, read-only."""

from __future__ import annotations

import hashlib
import io
import json
import platform
import shutil
import tarfile
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rrotord import VERSION
from rrotord.errors import BadRequest, NotFound, RrotorError
from rrotord.keys import verify

NAME = r"^[a-z][a-z0-9-]{0,39}$"
ARCHIVE_MAX = 256 * 1024 * 1024
# Which mod brings each part the base knows (by the mod's slug, as the licence names it): what a run is told when
# the part is missing. The database parts are rrotor's own and need no plan.
BASE_MOD = "rrotor"
KNOWN: dict[str, str] = {"ada": "ada", "postgres": BASE_MOD, "mongodb": BASE_MOD, "neo4j": BASE_MOD}


class PartInfo(BaseModel):
    """part.json, at the top of a part's tarball."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    version: str = Field(pattern=r"^[0-9]+(\.[0-9]+){0,2}$")
    mod: str = Field(pattern=NAME)
    arch: str = Field(pattern=r"^(amd64|arm64|any)$")
    rrotor: str = Field("0.0.0", pattern=r"^[0-9]+(\.[0-9]+){0,2}$")
    env: dict[str, list[str]] = {}
    services: list[str] = []


class Installed(BaseModel):
    info: PartInfo
    path: Path


def arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def _tuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


class Parts:
    def __init__(self, data: Path, release_key: Ed25519PublicKey | None, this_arch: str | None = None) -> None:
        self.root = data / "parts"
        self.key = release_key
        self.arch = this_arch or arch()

    def _current(self, name: str) -> Path:
        return self.root / name / "current"

    def get(self, name: str) -> Installed | None:
        current = self._current(name)
        if not current.is_dir():
            return None
        try:
            info = PartInfo.model_validate(json.loads((current / "part.json").read_text()))
        except (OSError, ValueError, ValidationError):
            return None
        return Installed(info=info, path=current.resolve())

    def installed(self) -> list[Installed]:
        if not self.root.is_dir():
            return []
        found = (self.get(p.name) for p in sorted(self.root.iterdir()) if p.is_dir())
        return [i for i in found if i is not None]

    def check(self, archive: bytes, sha256: str, signature: str) -> None:
        """The archive's hash and signature, before anything is unpacked."""
        if self.key is None:
            raise RrotorError(
                "This rrotor has no release key (RROTOR_RELEASE_PUBLIC_KEY), so no part installs.", code="E_CONFIG"
            )
        if len(archive) > ARCHIVE_MAX:
            raise BadRequest("The part is over 256 MB.", code="E_TOO_BIG")
        if hashlib.sha256(archive).hexdigest() != sha256.strip().lower():
            raise BadRequest("The part's SHA-256 is not the one given.", code="E_PART_HASH")
        if not verify(self.key, signature, archive):
            raise BadRequest("The part's signature does not check with Glyphh's release key.", code="E_PART_SIGNATURE")

    @staticmethod
    def read_info(archive: bytes) -> PartInfo:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                member = tar.extractfile("part.json")
                if member is None:
                    raise BadRequest("The part has no part.json at its top.", code="E_PART_MANIFEST")
                return PartInfo.model_validate(json.loads(member.read()))
        except (tarfile.TarError, KeyError, ValueError, ValidationError) as err:
            raise BadRequest(f"The part's manifest is refused: {err}", code="E_PART_MANIFEST") from None

    def fits(self, info: PartInfo, name: str, version: str) -> None:
        if (info.name, info.version) != (name, version):
            raise BadRequest(f"The part is {info.name} {info.version}, not {name} {version}.", code="E_PART_MANIFEST")
        if info.arch not in (self.arch, "any"):
            raise BadRequest(f"The part is for {info.arch}; this rrotor is {self.arch}.", code="E_PART_ARCH")
        if _tuple(info.rrotor) > _tuple(VERSION):
            raise BadRequest(f"The part needs rrotor {info.rrotor}; this is {VERSION}.", code="E_PART_RROTOR")

    def install(self, name: str, version: str, archive: bytes, sha256: str, signature: str) -> Installed:
        """The part checked, unpacked beside the versions it has, and made current in one rename."""
        self.check(archive, sha256, signature)
        info = self.read_info(archive)
        self.fits(info, name, version)
        home = self.root / name
        target, staging = home / version, home / f".{version}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                tar.extractall(staging, filter="data")
            shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        link = home / ".current.new"
        link.unlink(missing_ok=True)
        link.symlink_to(version, target_is_directory=True)
        link.replace(self._current(name))
        for old in home.iterdir():
            if old.is_dir() and not old.is_symlink() and old.name not in (version,) and not old.name.startswith("."):
                shutil.rmtree(old, ignore_errors=True)
        return Installed(info=info, path=target.resolve())

    def remove(self, name: str) -> None:
        if self.get(name) is None:
            raise NotFound(f"No part called {name} is installed.", code="E_PART_MISSING", part=name)
        shutil.rmtree(self.root / name)

    def resolve(self, names: list[str]) -> tuple[list[tuple[str, Path]], list[str]]:
        """The mount for each installed part named, and the names not installed."""
        mounts, missing = [], []
        for name in names:
            found = self.get(name)
            if found is None:
                missing.append(name)
            else:
                mounts.append((name, found.path))
        return mounts, missing

    def env(self, names: list[str], at: str) -> list[str]:
        """The PYTHONPATH entries the parts named ask for, under where they are mounted (`at`/<name>/...)."""
        out = []
        for name in names:
            found = self.get(name)
            for folder in found.info.env.get("PYTHONPATH", []) if found else []:
                out.append(f"{at}/{name}/{folder}")
        return out

    @staticmethod
    def view(installed: list[Installed]) -> list[dict[str, Any]]:
        return [
            {"name": i.info.name, "version": i.info.version, "mod": i.info.mod, "arch": i.info.arch} for i in installed
        ]
