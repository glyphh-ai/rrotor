"""The programs this rrotor holds: each version a checked-out commit with its venv, immutable, and which one is
active. On /data/programs/<name>/ (docs/architecture.md, 4); an earlier release's /data/models/ is moved there when
the daemon starts, if /data/programs/ is not there yet."""

from __future__ import annotations

import datetime as dt
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rrotord import manifest as manifests
from rrotord.errors import NotFound
from rrotord.store import read_json, write_json

BUILTIN = "builtin.json"  # in a built-in CLI's src/: its vendor, title and version


@dataclass(frozen=True, slots=True)
class Version:
    program: str
    version: int
    commit_sha: str
    deployed_at: str
    root: Path
    manifest: manifests.Manifest

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def venv(self) -> Path:
        return self.root / "venv"

    @property
    def builtin(self) -> dict[str, Any] | None:
        """A built-in CLI's vendor, title and pinned version (the image carries it, rrotord deployed it at boot);
        None for what was deployed."""
        found = read_json(self.src / BUILTIN)
        return found if isinstance(found, dict) else None

    def to_json(self) -> dict[str, Any]:
        return {
            "program": self.program,
            "version": self.version,
            "commit_sha": self.commit_sha,
            "deployed_at": self.deployed_at,
        }


class Programs:
    def __init__(self, data: Path) -> None:
        self.root = data / "programs"
        earlier = data / "models"  # an earlier release named a program `model`: its deploys are kept, moved once
        if earlier.is_dir() and not self.root.exists():
            earlier.rename(self.root)
        self.on_change: Callable[[str, Version], None] | None = None  # a version activated or removed (the head's)

    def folder(self, name: str) -> Path:
        return self.root / name

    def files(self, name: str) -> Path:
        return self.root / name / "files"

    def data(self, name: str) -> Path:
        return self.root / name / "data"

    def version_root(self, name: str, sha: str) -> Path:
        return self.root / name / "versions" / sha

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "versions").is_dir())

    def _load(self, name: str, root: Path) -> Version | None:
        meta = read_json(root / "version.json")
        if meta is None:
            return None
        return Version(
            name,
            int(meta["version"]),
            meta["commit_sha"],
            meta["deployed_at"],
            root,
            manifests.Manifest.model_validate(meta["manifest"]),
        )

    def versions(self, name: str) -> list[Version]:
        folder = self.root / name / "versions"
        found = [v for p in sorted(folder.iterdir()) if (v := self._load(name, p))] if folder.is_dir() else []
        return sorted(found, key=lambda v: v.version)

    def by_sha(self, name: str, sha: str) -> Version | None:
        return self._load(name, self.version_root(name, sha))

    def active(self, name: str) -> Version | None:
        pointer = read_json(self.root / name / "active.json")
        return self.by_sha(name, pointer["commit_sha"]) if pointer else None

    def get(self, name: str, version: int | None = None) -> Version:
        if name not in self.names():
            raise NotFound(f"This rrotor has no program {name}.", code="E_PROGRAM_MISSING", program=name)
        if version is None:
            found = self.active(name)
        else:
            found = next((v for v in self.versions(name) if v.version == version), None)
        if found is None:
            raise NotFound(
                f"{name} has no version {version or '(active)'} here.",
                code="E_VERSION_MISSING",
                program=name,
                version=version,
            )
        return found

    def record(self, name: str, version: int, sha: str, manifest: manifests.Manifest) -> Version:
        root = self.version_root(name, sha)
        meta = {
            "version": version,
            "commit_sha": sha,
            "deployed_at": dt.datetime.now(dt.UTC).isoformat(),
            "manifest": manifest.model_dump(mode="json"),
        }
        write_json(root / "version.json", meta)
        found = self._load(name, root)
        assert found is not None  # noqa: S101  (just written)
        return found

    def renumber(self, found: Version, version: int) -> Version:
        meta = read_json(found.root / "version.json")
        meta["version"] = version
        write_json(found.root / "version.json", meta)
        return self.by_sha(found.program, found.commit_sha) or found

    def next_number(self, name: str) -> int:
        return max((v.version for v in self.versions(name)), default=0) + 1

    def activate(self, found: Version) -> None:
        write_json(self.root / found.program / "active.json", {"commit_sha": found.commit_sha})
        if self.on_change is not None:
            self.on_change("activate", found)

    def remove(self, found: Version) -> None:
        active = self.active(found.program)
        if active is not None and active.commit_sha == found.commit_sha:
            (self.root / found.program / "active.json").unlink()
        shutil.rmtree(found.root)
        if self.on_change is not None:
            self.on_change("remove", found)

    def deployed(self) -> list[dict[str, Any]]:
        """The active version of each program, for the link's hello (not the built-in CLIs: the image's own)."""
        return [
            {"program": v.program, "version": v.version, "commit_sha": v.commit_sha}
            for n in self.names()
            if (v := self.active(n)) and v.builtin is None
        ]

    def clis(self) -> list[dict[str, Any]]:
        """The CLIs here, each by its active version: what it installs and runs, the arguments it allows and those
        that wait for the person's approval, how it signs in (cli_login), and a built-in one's vendor and version."""
        out = []
        for name in self.names():
            active = self.active(name)
            cli = active.manifest.cli if active else None
            if active is None or cli is None:
                continue
            out.append(
                {
                    "name": name,
                    "description": active.manifest.description,
                    "version": active.version,
                    "command": cli.command,
                    "install": {"source": cli.install.source, "pinned": cli.install.pinned},
                    "allow": cli.allow,
                    "approvals": cli.approvals,
                    "needs_approval": bool(cli.approvals),
                    "home": cli.home,
                    "hosts": active.manifest.hosts,
                    "login": {"args": cli.login.args} if cli.login else None,
                    "builtin": active.builtin,
                }
            )
        return out

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for name in self.names():
            active = self.active(name)
            versions = self.versions(name)
            shown = active or (versions[-1] if versions else None)
            out.append(
                {
                    "name": name,
                    "kind": shown.manifest.kind if shown else "program",
                    "description": shown.manifest.description if shown else "",
                    "active": active.version if active else None,
                    "functions": {k: f.model_dump() for k, f in shown.manifest.functions.items()} if shown else {},
                    "versions": [v.to_json() for v in versions],
                }
            )
        return out
