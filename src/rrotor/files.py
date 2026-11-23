"""The files of the machine's folders a run was given (`ctx.folders`; docs/architecture.md, 5.11), for a program that
lets something else choose the paths (a model's tool calls): list a folder, read a file, search by name or text,
write a file and move one. Standard library only.

    files = Files(ctx.folders)
    files.read("/Users/ana/Invoices/March.txt").text
    files.write("/Users/ana/Notes/Summary.md", "...")     # only in a folder given to change

Every call checks its path against the folders given, by its real path: a path outside them, one with `..`, and one
that is a link or is reached through one are refused (`FileRefused`, a plain sentence), and so is a change in a
folder given to read. The sandbox is the hard wall (it mounts exactly those folders, read-only or writable); this
check is the polite one, and says why. There is no delete and no shell. `before` is called with each `Change` about
to be made, before anything is touched: a program asks the person there (or raises to refuse). Reading is bounded:
`read_max` bytes a `Files` (a run), `FILE_MAX` a file. `touched` names what was read and changed, never what is in
it: `[{"name": "Invoices/March.txt", "did": "read"}]`."""

from __future__ import annotations

import html
import os
import re
import shutil
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rrotor.errors import ProgramError

READ_MAX = 8 * 1024 * 1024  # the bytes one Files (a run) reads in all
FILE_MAX = 4 * 1024 * 1024  # the biggest file read
TEXT_MAX = 200_000  # the characters of a file's text given back; a longer one is cut, and says so
WRITE_MAX = 1024 * 1024  # the biggest file written
LIST_MAX = 500  # the entries one listing answers
FOUND_MAX = 50  # the matches one search answers
WALK_MAX = 5000  # the files one search looks at
SCAN_MAX = 512 * 1024  # the biggest file a search reads for its text
LINE_MAX = 200  # the characters of a matching line given back
WORD = "word/document.xml"


class FileRefused(ProgramError):
    """A path or a change the folders given do not allow, or a file that cannot be read: said in a plain sentence."""

    code = "E_FILE_REFUSED"


@dataclass(frozen=True, slots=True)
class Change:
    """A change about to be made: `action` `write` or `move`, the file's real `path` and its `name` as a person
    reads it (`Invoices/March.txt`); for a move, where it goes (`to`, `to_name`)."""

    action: str
    path: str
    name: str
    to: str | None = None
    to_name: str | None = None

    def view(self) -> dict[str, str]:
        said = {"action": self.action, "path": self.path, "name": self.name}
        return said | ({"to": self.to, "to_name": self.to_name} if self.to and self.to_name else {})


@dataclass(frozen=True, slots=True)
class Read:
    """A file read: `kind` `text` (its `text`, cut at TEXT_MAX when `truncated`) or `pdf` (its bytes, `data`: a
    PDF's text is not taken out here)."""

    name: str
    kind: str
    size: int
    text: str = ""
    data: bytes = b""
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class _Folder:
    given: str  # as the run was given it
    real: str  # its real path
    write: bool


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _word(path: str) -> str:
    """A Word document's text (.docx: a zip whose word/document.xml holds its paragraphs), by the standard library."""
    try:
        with zipfile.ZipFile(path) as z:
            if z.getinfo(WORD).file_size > FILE_MAX * 4:
                raise FileRefused(f"{Path(path).name} is too big to read.")
            xml = z.read(WORD).decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        raise FileRefused(f"{Path(path).name} is not a Word document that can be read here.") from None
    xml = re.sub(r"<w:tab\b[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br\b[^>]*/>|</w:p>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()


class Files:
    """The files of the folders a run was given. `folders` is `ctx.folders`; `before` is called with each Change
    before it is made; `read_max` bounds the bytes read in all."""

    def __init__(
        self,
        folders: Iterable[Mapping[str, str]],
        *,
        before: Callable[[Change], None] | None = None,
        read_max: int = READ_MAX,
    ) -> None:
        given = [(os.path.normpath(str(f["path"])), f.get("access") == "write") for f in folders]
        found = [_Folder(path, os.path.realpath(path), write) for path, write in given]
        self._folders = sorted(found, key=lambda f: len(f.real), reverse=True)  # the folder inside another first
        self._before = before
        self.read_max, self.read_bytes = read_max, 0
        self.touched: list[dict[str, str]] = []

    # paths

    def folders(self) -> list[dict[str, str]]:
        """The folders given: `[{path, name, access}]`."""
        given = (
            {"path": f.given, "name": Path(f.given).name, "access": "write" if f.write else "read"}
            for f in self._folders
        )
        return sorted(given, key=lambda f: f["path"])

    def _place(self, path: Any, write: bool = False) -> tuple[str, _Folder]:
        """The real path `path` names and the folder given that holds it; FileRefused for any other path."""
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise FileRefused(f"{path!r} is not a path: name a file or folder by its whole path, from /.")
        if ".." in path.split("/"):
            raise FileRefused(f"{path} has '..' in it: name the file itself.")
        lexical = os.path.normpath(path)
        shares = ", ".join(f.given for f in sorted(self._folders, key=lambda f: f.given)) or "none"
        bases = ((f, base) for f in self._folders for base in (f.given, f.real) if _within(lexical, base))
        outer, base = next(bases, (None, ""))
        if outer is None:
            raise FileRefused(f"{path} is not in a folder this bot was given: it has {shares}.")
        wanted = outer.real + lexical[len(base) :]
        real = os.path.realpath(wanted)
        if real != wanted:
            raise FileRefused(f"{path} is a link, or is reached through one: name the file it points to, if given.")
        folder = next(f for f in self._folders if _within(real, f.real))
        if write and not folder.write:
            raise FileRefused(f"{folder.given} was given to read only: nothing in it can be changed.")
        return real, folder

    def name(self, real: str) -> str:
        """A path as a person reads it: its folder's name and the way down (`Invoices/2025/March.txt`)."""
        folder = next(f for f in self._folders if _within(real, f.real))
        rel = os.path.relpath(real, folder.real)
        return Path(folder.given).name if rel == "." else f"{Path(folder.given).name}/{rel}"

    def _did(self, name: str, did: str) -> None:
        self.touched = [t for t in self.touched if (t["name"], t["did"]) != (name, did)] + [{"name": name, "did": did}]

    # reading

    def list(self, path: str | None = None) -> dict[str, Any]:
        """What is in a folder: `{folder, entries: [{name, path, kind: folder | file, size}], more}`, folders first;
        links are left out. With no path: the folders given."""
        if path is None:
            return {"folders": self.folders()}
        real, _ = self._place(path)
        if not os.path.isdir(real):
            raise FileRefused(f"{path} is not a folder.")
        entries: list[dict[str, Any]] = []
        with os.scandir(real) as found:
            for entry in sorted(found, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())):
                if entry.is_symlink():
                    continue
                folder = entry.is_dir(follow_symlinks=False)
                size = None if folder else entry.stat(follow_symlinks=False).st_size
                entries.append({"name": entry.name, "path": entry.path, "kind": "folder" if folder else "file"})
                if size is not None:
                    entries[-1]["size"] = size
        return {"folder": self.name(real), "entries": entries[:LIST_MAX], "more": max(0, len(entries) - LIST_MAX)}

    def read(self, path: str) -> Read:
        """A file's text (plain text, or a Word document's), or a PDF's bytes; FileRefused for any other kind, a
        file past FILE_MAX, or once this run has read `read_max` bytes."""
        real, _ = self._place(path)
        if not os.path.isfile(real):
            raise FileRefused(f"{path} is not a file.")
        size, name = os.path.getsize(real), self.name(real)
        if size > FILE_MAX:
            raise FileRefused(f"{name} is {size:,} bytes: a file of at most {FILE_MAX:,} can be read.")
        if self.read_bytes + size > self.read_max:
            raise FileRefused(f"This run has read its limit of {self.read_max:,} bytes: {name} was not read.")
        suffix = Path(real).suffix.lower()
        if suffix == ".docx":
            read = Read(name, "text", size, *self._cut(_word(real)))
        else:
            data = Path(real).read_bytes()
            if suffix == ".pdf" or data.startswith(b"%PDF-"):
                read = Read(name, "pdf", size, data=data)
            else:
                read = Read(name, "text", size, *self._cut(self._text(data, name)))
        self.read_bytes += size
        self._did(name, "read")
        return read

    @staticmethod
    def _text(data: bytes, name: str) -> str:
        try:
            if b"\x00" in data:
                raise ValueError(name)
            return data.decode("utf-8-sig")
        except ValueError:
            raise FileRefused(
                f"{name} is not text: files of its type cannot be read here (text, Word .docx and PDF can)."
            ) from None

    @staticmethod
    def _cut(text: str) -> tuple[str, bytes, bool]:
        return text[:TEXT_MAX], b"", len(text) > TEXT_MAX

    def _walk(self, top: str) -> Iterator[str]:
        for folder, inner, names in os.walk(top, followlinks=False):
            inner[:] = sorted(d for d in inner if not os.path.islink(os.path.join(folder, d)))
            for name in sorted(names):
                found = os.path.join(folder, name)
                if not os.path.islink(found):
                    yield found

    def search(self, query: str, under: str | None = None) -> dict[str, Any]:
        """The files whose name has `query`, and the lines of text files that have it (whatever its case), in
        `under` or every folder given: `{matches: [{name, path, line?, text?}], more}`, FOUND_MAX at most."""
        wanted = query.strip().lower() if isinstance(query, str) else ""
        if not wanted:
            raise FileRefused("A search needs some text to look for.")
        tops = [self._place(under)[0]] if under is not None else sorted({f.real for f in self._folders})
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        looked = 0
        for top in tops:
            for found in self._walk(top):
                if found in seen or looked >= WALK_MAX:
                    continue
                seen.add(found)
                looked += 1
                name = self.name(found)
                if wanted in os.path.basename(found).lower():
                    matches.append({"name": name, "path": found})
                matches.extend({"name": name, "path": found, **hit} for hit in self._lines(found, wanted))
        return {"matches": matches[:FOUND_MAX], "more": max(0, len(matches) - FOUND_MAX)}

    @staticmethod
    def _lines(path: str, wanted: str) -> Iterator[dict[str, Any]]:
        try:
            if os.path.getsize(path) > SCAN_MAX:
                return
            data = Path(path).read_bytes()
        except OSError:
            return
        if b"\x00" in data:
            return
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if wanted in line.lower():
                yield {"line": n, "text": line.strip()[:LINE_MAX]}

    # changing

    def _change(self, change: Change) -> None:
        if self._before is not None:
            self._before(change)

    def write(self, path: str, content: str) -> dict[str, Any]:
        """A file written whole (made, with its folders, when it is not there), in a folder given to change:
        `{name, bytes, created}`."""
        if not isinstance(content, str):
            raise FileRefused("A file is written as text.")
        data = content.encode("utf-8")
        if len(data) > WRITE_MAX:
            raise FileRefused(f"That is {len(data):,} bytes: a file of at most {WRITE_MAX:,} can be written.")
        real, _ = self._place(path, write=True)
        if os.path.isdir(real):
            raise FileRefused(f"{path} is a folder, not a file.")
        created, name = not os.path.exists(real), self.name(real)
        self._change(Change("write", real, name))
        os.makedirs(os.path.dirname(real), exist_ok=True)
        opened = os.open(real, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(opened, "wb") as f:
            f.write(data)
        self._did(name, "written")
        return {"name": name, "bytes": len(data), "created": created}

    def move(self, path: str, to: str) -> dict[str, Any]:
        """A file moved or renamed, both ends in folders given to change; never onto a file that is there:
        `{name, to}`."""
        real, _ = self._place(path, write=True)
        target, _ = self._place(to, write=True)
        if not os.path.isfile(real):
            raise FileRefused(f"{path} is not a file: only a file is moved or renamed.")
        if os.path.lexists(target):
            raise FileRefused(f"{to} is there already: nothing is moved onto a file or folder that exists.")
        name, to_name = self.name(real), self.name(target)
        self._change(Change("move", real, name, target, to_name))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(real, target)
        self._did(to_name, "moved")
        return {"name": name, "to": to_name}
