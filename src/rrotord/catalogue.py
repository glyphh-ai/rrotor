"""What the Glyphh catalogue says of this rrotor's apps (docs/architecture.md, 5.8; the server's
docs/specs/apps.md, 3.4): each app's reviewed version and its state (draft, approved, rejected, retired) and the
roles that may open it, told down the link (`app` messages, and the welcome's whole list) and kept in /data/apps.json
so a restart serves the same; and the reviewer groups, who may open a draft. With the link off there is no
catalogue: a deploy is the approval, and the manifest's roles decide.

rrotor 0.2.0 kept the same file as /data/artifacts.json: when apps.json is not there yet and artifacts.json is, it
is read once and written as apps.json (the one line of compatibility; the old file is left alone)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rrotord.store import read_json, write_json

STATES = ("draft", "approved", "rejected", "retired")
ADMIN = "admin"
FORMER = "artifacts.json"  # 0.2.0's name for the file, taken over once


class Catalogue:
    def __init__(self, data: Path, linked: bool = True) -> None:
        self.path = data / "apps.json"
        self.linked = linked
        kept = read_json(self.path)
        if kept is None:  # not written yet: what a 0.2.0 left, if anything, taken over
            kept = read_json(data / FORMER)
            if kept is not None:
                write_json(self.path, kept)
        kept = kept or {}
        self.entries: dict[str, dict[str, Any]] = dict(kept.get("apps") or {})
        self.reviewers: list[str] = list(kept.get("reviewers") or [])

    def _save(self) -> None:
        write_json(self.path, {"apps": self.entries, "reviewers": self.reviewers})

    def set_all(self, entries: list[dict[str, Any]], reviewers: list[str] | None = None) -> None:
        """The welcome's whole list: what the catalogue holds for this rrotor now, nothing else."""
        self.entries = {str(e["program"]): self._entry(e) for e in entries}
        if reviewers is not None:
            self.reviewers = [r.lower() for r in reviewers]
        self._save()

    def set_one(self, program: str, version: int, state: str, roles: list[str]) -> None:
        self.entries[program] = self._entry({"version": version, "state": state, "roles": roles})
        self._save()

    @staticmethod
    def _entry(e: dict[str, Any]) -> dict[str, Any]:
        return {"version": int(e["version"]), "state": str(e["state"]), "roles": [str(r).lower() for r in e["roles"]]}

    def get(self, program: str) -> dict[str, Any] | None:
        return self.entries.get(program)

    def state(self, program: str, version: int) -> str:
        """The catalogue's word on this version of the app: its state when it is the reviewed version, `draft` when a
        newer version than the reviewed one, `unknown` when the catalogue has not spoken (or the link is off)."""
        if not self.linked:
            return "unknown"
        entry = self.entries.get(program)
        if entry is None:
            return "unknown"
        if entry["version"] == version:
            return str(entry["state"])
        return "draft" if version > int(entry["version"]) else "retired"

    def approved(self, program: str, version: int) -> bool:
        """Whether this version may be served to the org's people: approved by the catalogue, or, with the link off,
        deployed at all."""
        return not self.linked or self.state(program, version) == "approved"

    def is_reviewer(self, roles: set[str]) -> bool:
        lowered = {r.lower() for r in roles}
        return ADMIN in lowered or bool(set(self.reviewers) & lowered)
