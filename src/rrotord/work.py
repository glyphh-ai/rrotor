"""The work on this machine, read where it is: the crew's clones (the repos `glyphh_swarm` works in, in its data
folder's `repos/`), each a workspace whose files, diffs and command log a person looks at from Glyphh's platform, in
their own browser, at this rrotor's own address (docs/architecture.md, 7.1). None of it goes up the link or to
Glyphh's server: the answers are the files' own bytes, and they stay between the machine and the browser.

A workspace's id is its folder's name, `<owner>__<repo>`. What is read: its folders (never `.git`), a file (from the
working tree, or as a branch has it), the diff of a branch (or of the working tree) against where it left its base,
and the command log `glyphh_swarm` keeps of each turn (`terminal/<id>.jsonl`: what git and Claude Code ran, read
only; nothing is ever run for a caller). What is written: one file of the working tree, never while a turn holds the
clone (its lock, `repos/<id>.lock`, the one a turn takes), never `.git`, never outside the workspace.

git is run with fixed arguments (never a shell), no system or global config, no hooks, no fsmonitor, no external
diff and no textconv, with a timeout: a clone's own config cannot make a read run anything."""

from __future__ import annotations

import contextlib
import fcntl
import json
import mimetypes
import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from rrotord.errors import BadRequest, Conflict, NotFound, RrotorError

PROGRAM = "glyphh_swarm"  # whose data folder holds the clones (programs/glyphh_swarm: clone_of, held)
WS = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+$")
BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<![./])$")  # glyphh_swarm's own
TEXT_MAX = 1 << 20  # a file's text, read or written
RAW_MAX = 10 << 20  # a file's bytes
SIDE_MAX = 400 * 1024  # one side of one file of a diff
FILES_MAX = 200  # files in a diff
ENTRIES_MAX = 2000  # entries of one folder
BRANCHES_MAX = 50
LINES_MAX = 1000  # lines of the command log in one answer
GIT_SECONDS = 20
SAFE = ("-c", "safe.directory=*", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
QUIET = ("-c", "core.quotePath=false")
DIFF = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "-z")
STATUS = {"A": "added", "D": "deleted"}


class Busy(Conflict):
    code = "E_BUSY"


class TooBig(RrotorError):
    code, status = "E_TOO_BIG", 413


def bad_path(rel: str) -> BadRequest:
    return BadRequest(f"Not a path inside the workspace: {rel!r}.", code="E_PATH")


def hidden(parts: tuple[str, ...]) -> bool:
    """Whether a path goes through `.git` (in any case: a case-insensitive disk reads `.GIT` as it)."""
    return any(p.lower() == ".git" for p in parts)


def inside(folder: Path, rel: str, root_ok: bool = False) -> Path:
    """The real path `rel` names under a workspace: relative, never through `..` or `.git`, and, once its symlinks
    are followed, still inside the workspace and still not `.git`. Refused E_PATH otherwise."""
    parts = PurePosixPath(rel).parts
    if "\x00" in rel or rel.startswith("/") or ".." in parts or hidden(parts) or not (parts or root_ok):
        raise bad_path(rel)
    root, path = folder.resolve(), (folder / rel).resolve()
    if not path.is_relative_to(root) or hidden(path.relative_to(root).parts):
        raise bad_path(rel)
    return path


def branch_name(name: str, what: str) -> str:
    if not BRANCH.match(name) or name.endswith(".lock"):
        raise BadRequest(f"{what} is not a branch name: {name!r}.", code="E_BRANCH")
    return name


def as_text(data: bytes | None, limit: int) -> tuple[str | None, bool, bool]:
    """Bytes as (text, binary, truncated): the first `limit` bytes as UTF-8; no text for a binary file or one too big
    to have been read at all."""
    if data is None:
        return None, False, True
    chunk, cut = data[:limit], len(data) > limit
    if b"\x00" in chunk:
        return None, True, cut
    try:
        return chunk.decode("utf-8", "ignore" if cut else "strict"), False, cut
    except UnicodeDecodeError:
        return None, True, cut


class Work:
    def __init__(self, data: Path) -> None:
        self.repos = data / "repos"
        self.logs = data / "terminal"
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": os.devnull,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }

    # git, and the clone's lock

    def git(self, folder: Path, *args: str) -> bytes | None:
        """git's output for these arguments in a clone; None when it fails or does not answer in time."""
        try:
            done = subprocess.run(
                ["git", *SAFE, *QUIET, *args],
                cwd=folder,
                env=self.env,
                capture_output=True,
                timeout=GIT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return done.stdout if done.returncode == 0 else None

    def said(self, folder: Path, *args: str) -> str | None:
        out = self.git(folder, *args)
        return None if out is None else out.decode("utf-8", "replace").strip()

    def folder(self, ws: str) -> Path:
        path = self.repos / ws
        if not WS.match(ws) or not (path / ".git").is_dir():
            raise NotFound(f"There is no workspace {ws!r} on this rrotor.")
        return path

    @contextlib.contextmanager
    def free(self, folder: Path) -> Iterator[None]:
        """The clone held as a turn holds it, for as long as a write takes; refused E_BUSY while a turn has it."""
        with open(folder.with_suffix(".lock"), "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy("A turn is at work in this workspace: try again when it ends.") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def live(self, folder: Path) -> bool:
        """Whether a turn holds the clone right now."""
        try:
            with self.free(folder):
                return False
        except Busy:
            return True

    # what is read

    def workspaces(self) -> dict[str, Any]:
        found: list[dict[str, Any]] = []
        for path in sorted(self.repos.iterdir()) if self.repos.is_dir() else []:
            if not WS.match(path.name) or not (path / ".git").is_dir():
                continue
            head = self.said(path, "rev-parse", "--abbrev-ref", "HEAD")
            listed = self.said(
                path,
                "for-each-ref",
                "--sort=-committerdate",
                f"--count={BRANCHES_MAX}",
                "--format=%(refname:short)",
                "refs/heads",
            )
            found.append(
                {
                    "id": path.name,
                    "name": path.name.replace("__", "/", 1),
                    "kind": "repo",
                    "branch": head if head and head != "HEAD" else None,
                    "branches": (listed or "").split(),
                }
            )
        return {"workspaces": found}

    def tree(self, ws: str, under: str) -> dict[str, Any]:
        """One folder's entries: folders first, then by name; `.git` is never among them."""
        folder = self.folder(ws)
        base, root = inside(folder, under, root_ok=True), folder.resolve()
        if not base.is_dir():
            raise NotFound(f"There is no folder {under!r} in this workspace.")
        entries: list[dict[str, Any]] = []
        with os.scandir(base) as listing:
            for entry in listing:
                if entry.name.lower() == ".git":
                    continue
                is_dir, is_file = entry.is_dir(follow_symlinks=False), entry.is_file(follow_symlinks=False)
                entries.append(
                    {
                        "name": entry.name,
                        "path": (base / entry.name).relative_to(root).as_posix(),
                        "kind": "dir" if is_dir else "file",
                        "size": entry.stat(follow_symlinks=False).st_size if is_file else None,
                    }
                )
        entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower(), e["name"]))
        return {
            "under": base.relative_to(root).as_posix() if base != root else "",
            "entries": entries[:ENTRIES_MAX],
            "truncated": len(entries) > ENTRIES_MAX,
        }

    def blob(self, folder: Path, ref: str, rel: str, limit: int) -> tuple[bytes | None, int] | None:
        """A file as a commit has it: (its bytes, or None past `limit`; its size); None when it is not there."""
        size = self.said(folder, "cat-file", "-s", f"{ref}:{rel}")
        if size is None or not size.isdigit():
            return None
        if int(size) > limit:
            return None, int(size)
        return self.git(folder, "cat-file", "blob", f"{ref}:{rel}"), int(size)

    def read(self, ws: str, rel: str, ref: str | None, limit: int | None = None) -> tuple[bytes | None, int]:
        """A file's bytes (None past `limit`, RAW_MAX unless given) and its size: the working tree's, or as the branch
        `ref` has it."""
        folder, limit = self.folder(ws), RAW_MAX if limit is None else limit
        path = inside(folder, rel)
        if ref is not None:
            got = self.blob(folder, branch_name(ref, "ref"), PurePosixPath(rel).as_posix(), limit)
            if got is None:
                raise NotFound(f"There is no file {rel!r} on {ref}.")
            return got
        if not path.is_file():
            raise NotFound(f"There is no file {rel!r} in this workspace.")
        size = path.stat().st_size
        return (path.read_bytes() if size <= limit else None), size

    def file(self, ws: str, rel: str, ref: str | None = None) -> dict[str, Any]:
        data, size = self.read(ws, rel, ref)
        text, binary, truncated = as_text(data, TEXT_MAX)
        return {
            "path": PurePosixPath(rel).as_posix(),
            "size": size,
            "binary": binary,
            "text": text,
            "truncated": truncated,
        }

    def raw(self, ws: str, rel: str, ref: str | None = None) -> tuple[bytes, str]:
        """A file's own bytes and the content type its name suggests (an image for the chat, a page for Preview)."""
        data, size = self.read(ws, rel, ref)
        if data is None:
            raise TooBig(f"{rel} is {size} bytes: more than this rrotor hands a browser ({RAW_MAX}).")
        return data, mimetypes.guess_type(rel)[0] or "application/octet-stream"

    def write(self, ws: str, rel: str, text: str) -> dict[str, Any]:
        """One file of the working tree, written whole; its folders made. Never while a turn holds the clone."""
        folder = self.folder(ws)
        path, raw = inside(folder, rel), text.encode()
        if len(raw) > TEXT_MAX:
            raise TooBig(f"A file saved from the editor is at most {TEXT_MAX} bytes.")
        if path.is_dir():
            raise bad_path(rel)
        with self.free(folder):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        return {"path": path.relative_to(folder.resolve()).as_posix(), "size": len(raw)}

    # the diff

    def base_of(self, folder: Path, base: str | None) -> str:
        """The base branch: the one named, else the one GitHub's HEAD is on, else main."""
        if base:
            return branch_name(base, "base")
        head = self.said(folder, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or ""
        return head.removeprefix("origin/") or "main"

    def changed(self, folder: Path, since: str, branch: str | None) -> list[tuple[str, str, str, str]]:
        """(path, status, added, removed) of each file changed since a commit: on `branch`, or in the working tree,
        where a new file git does not track yet counts as added."""
        ends = [since, *([branch] if branch else []), "--"]
        names = (self.said(folder, *DIFF, "--name-status", *ends) or "").split("\x00")
        counts: dict[str, tuple[str, str]] = {}
        for record in (self.said(folder, *DIFF, "--numstat", *ends) or "").split("\x00"):
            added, _, rest = record.partition("\t")
            removed, _, path = rest.partition("\t")
            counts[path] = (added, removed)
        found = [
            (path, STATUS.get(letter[:1], "modified"), *counts.get(path, ("0", "0")))
            for letter, path in zip(names[::2], names[1::2], strict=False)
        ]
        if branch is None:
            new = self.said(folder, "ls-files", "--others", "--exclude-standard", "-z") or ""
            found += [(path, "added", "", "0") for path in new.split("\x00") if path]
        return found

    def side(self, folder: Path, ref: str | None, rel: str) -> tuple[bytes | None, bool]:
        """One side of a changed file, and whether it was too big to carry: as `ref` has it, or the working tree's."""
        if ref is not None:
            got = self.blob(folder, ref, rel, SIDE_MAX)
            return (None, False) if got is None else (got[0], got[0] is None)
        path = folder / rel
        if path.is_symlink() or not path.is_file():
            return None, False
        return (None, True) if path.stat().st_size > SIDE_MAX else (path.read_bytes(), False)

    def one(self, folder: Path, since: str, branch: str | None, change: tuple[str, str, str, str]) -> dict[str, Any]:
        path, status, added, removed = change
        before, cut_before = (None, False) if status == "added" else self.side(folder, since, path)
        after, cut_after = (None, False) if status == "deleted" else self.side(folder, branch, path)
        old, old_binary, _ = as_text(before, SIDE_MAX) if before is not None else (None, False, False)
        new, new_binary, _ = as_text(after, SIDE_MAX) if after is not None else (None, False, False)
        binary = added == "-" or old_binary or new_binary
        return {
            "path": path,
            "status": status,
            "added": int(added) if added.isdigit() else (new or "").count("\n") if not added else 0,
            "removed": int(removed) if removed.isdigit() else 0,
            "binary": binary,
            "before": None if binary else old,
            "after": None if binary else new,
            "truncated": cut_before or cut_after,
        }

    def diff(self, ws: str, branch: str | None = None, base: str | None = None) -> dict[str, Any]:
        """What a branch (or, with none named, the working tree) changed since it left its base: each file, both
        sides."""
        folder = self.folder(ws)
        base = self.base_of(folder, base)
        target = branch_name(branch, "branch") if branch else None
        since = self.said(folder, "merge-base", f"origin/{base}", target or "HEAD")
        if not since:
            raise NotFound(f"There is no {target or 'work'} to compare with {base} in this workspace.")
        changes = self.changed(folder, since, target)
        files = [self.one(folder, since, target, change) for change in changes[:FILES_MAX]]
        cuts = [f.pop("truncated") for f in files]
        return {"base": base, "branch": target, "files": files, "truncated": len(changes) > FILES_MAX or any(cuts)}

    # the command log

    def terminal(self, ws: str, after: int = 0) -> dict[str, Any]:
        """The command log's lines from number `after` on (the newest LINES_MAX of them), the number to ask from
        next, and whether a turn is at work now."""
        folder = self.folder(ws)
        try:
            written = (self.logs / f"{ws}.jsonl").read_text("utf-8", "replace").splitlines()
        except OSError:
            written = []
        lines: list[dict[str, Any]] = []
        for raw in written:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and isinstance(line.get("n"), int):
                lines.append({"n": line["n"], "at": line.get("at"), "kind": line.get("kind"), "text": line.get("text")})
        return {
            "lines": [line for line in lines if line["n"] >= after][-LINES_MAX:],
            "next": lines[-1]["n"] + 1 if lines else 0,
            "live": self.live(folder),
        }
