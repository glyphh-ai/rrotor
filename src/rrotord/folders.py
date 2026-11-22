"""The folders of this machine a program's run may be given (docs/architecture.md, 5.11). A run sees nothing of the
machine's files but its own folders; a call may carry `folders`, `[{path, access: "read" | "write"}]` (a bot's, chosen
by its owner on the platform), and the run then has exactly those mounted in its sandbox, at their own paths,
read-only or writable as chosen.

rrotor's owner says which folders may be given at all: `RROTOR_FOLDERS`, absolute paths joined by `:`. The default is
none: until the owner names one, every call that asks for a folder is refused. A folder asked for must be one of
those roots or lie inside one, exist, and be named by its real path (no symlink in it: a link out of a root is not
a way in); anything else is refused, saying why in a plain sentence. A root is never the rrotor's own data folder
(its secrets and keys), a folder holding it, or a path the sandbox keeps for itself.

`GET /v1/folders?under=<path>` reads the same roots for the platform's folder picker: the roots themselves, or the
folders inside one of them. Only folders are listed, never files or what is in them."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rrotord.errors import BadRequest, Forbidden, RrotorError

MAX = 20  # the folders one call may carry
LISTED_MAX = 1000  # the folders one listing answers
ACCESS = ("read", "write")
# what the sandbox mounts for itself (rrotord/sandbox/bwrap.py): never a person's folder
RESERVED = tuple(
    Path(p)
    for p in (
        *("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64", "/proc", "/dev", "/sys", "/tmp", "/run", "/boot"),  # noqa: S108
        *("/program", "/venv", "/opt", "/files", "/data", "/cli", "/work", "/home/cli"),
    )
)


@dataclass(frozen=True, slots=True)
class Grant:
    path: str
    write: bool

    def view(self) -> dict[str, str]:
        return {"path": self.path, "access": "write" if self.write else "read"}


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def roots_of(text: str, kept: Path) -> tuple[Path, ...]:
    """RROTOR_FOLDERS as the roots it names, or E_CONFIG saying which one cannot be offered."""
    found: list[Path] = []
    for part in (p.strip() for p in text.split(os.pathsep)):
        if not part:
            continue
        path = Path(part)
        if not path.is_absolute() or ".." in path.parts:
            raise RrotorError(
                f"RROTOR_FOLDERS names absolute paths joined by {os.pathsep!r}: {part!r} is not one.", code="E_CONFIG"
            )
        real = Path(os.path.realpath(path))
        own = within(kept, real) or within(real, kept)  # rrotor's data (and so the whole machine, which holds it)
        if own or any(within(real, r) for r in RESERVED):
            raise RrotorError(
                f"RROTOR_FOLDERS cannot offer {part}: it is the whole machine, rrotor's own data, or a path a run's "
                "sandbox keeps for itself.",
                code="E_CONFIG",
            )
        if real not in found:
            found.append(real)
    return tuple(found)


class Folders:
    def __init__(self, roots: Iterable[Path] = ()) -> None:
        self.roots = tuple(roots)

    def _refused(self, path: str) -> Forbidden:
        shared = ", ".join(str(r) for r in self.roots)
        offers = f"it shares {shared}" if shared else "it shares none (RROTOR_FOLDERS is not set)"
        return Forbidden(
            f"{path} is not in a folder this rrotor's owner shares with bots: {offers}.", code="E_FOLDER_REFUSED"
        )

    def inside(self, asked: Any) -> Path:
        """The folder asked for, when it is one of the roots or inside one; else why not."""
        if not isinstance(asked, str) or not asked.startswith("/") or "\x00" in asked:
            raise BadRequest("A folder is named by its absolute path.", code="E_FOLDER")
        path = Path(asked)
        if ".." in path.parts:
            raise self._refused(asked)
        real = Path(os.path.realpath(path))
        if not any(within(real, root) for root in self.roots):
            raise self._refused(asked)
        if not real.is_dir():
            raise Forbidden(f"{asked} is not a folder on this rrotor.", code="E_FOLDER_REFUSED")
        if real != Path(os.path.normpath(path)):
            raise Forbidden(f"{asked} is a link to {real}: name that folder itself.", code="E_FOLDER_REFUSED")
        return real

    def grants(self, asked: Any) -> tuple[Grant, ...]:
        """A call's `folders` as what its run is given, parents first; refused whole when any one is."""
        if asked is None:
            return ()
        if not isinstance(asked, list) or len(asked) > MAX:
            raise BadRequest(f"folders is a list of at most {MAX} of {{path, access}}.", code="E_FOLDER")
        given: dict[str, bool] = {}
        for one in asked:
            if not isinstance(one, dict) or one.get("access") not in ACCESS:
                raise BadRequest('A folder is {path, access: "read" or "write"}.', code="E_FOLDER")
            path = str(self.inside(one.get("path")))
            given[path] = given.get(path, False) or one["access"] == "write"
        return tuple(Grant(path, write) for path, write in sorted(given.items()))

    def listing(self, under: str | None) -> dict[str, Any]:
        """The picker's read: the roots, or the folders inside `under` (itself in a root), by name. Never a file, a
        hidden folder or a link."""
        if under is None:
            return {"folders": [{"path": str(r), "name": r.name or str(r)} for r in self.roots if r.is_dir()]}
        parent = self.inside(under)
        try:
            with os.scandir(parent) as found:
                names = sorted(e.name for e in found if not e.name.startswith(".") and e.is_dir(follow_symlinks=False))
        except OSError:
            raise Forbidden(f"rrotor cannot read {under}.", code="E_FOLDER_REFUSED") from None
        return {"folders": [{"path": str(parent / n), "name": n} for n in names[:LISTED_MAX]]}
