"""rrotor.files: the files of the folders a run was given, each path checked against them (docs/architecture.md,
5.11). The real sandbox's own say is tests/sandbox/test_files_sandbox.py."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from rrotor import files as filesmod
from rrotor.files import Change, FileRefused, Files


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = Path(os.path.realpath(tmp_path))
    for folder in ("docs/2025", "out/kept", "private"):
        (root / folder).mkdir(parents=True)
    (root / "docs" / "note.txt").write_text("alpha\nInvoice 7 is due\n")
    (root / "docs" / "2025" / "march.md").write_text("# March\nthe invoice was paid\n")
    (root / "out" / "kept" / "old.txt").write_text("kept")
    (root / "private" / "secret.txt").write_text("secret")
    return root


def given(home: Path, **more) -> Files:
    folders = [
        {"path": str(home / "docs"), "access": "read"},
        {"path": str(home / "out"), "access": "write"},
        {"path": str(home / "out" / "kept"), "access": "read"},
    ]
    return Files(folders, **more)


def refused(fn, *args) -> str:
    with pytest.raises(FileRefused) as err:
        fn(*args)
    assert err.value.code == "E_FILE_REFUSED"
    return err.value.message


def test_the_folders_given_and_what_is_in_one(home):
    files = given(home)
    assert files.list() == {
        "folders": [
            {"path": str(home / "docs"), "name": "docs", "access": "read"},
            {"path": str(home / "out"), "name": "out", "access": "write"},
            {"path": str(home / "out" / "kept"), "name": "kept", "access": "read"},
        ]
    }
    (home / "docs" / "link").symlink_to(home / "private")
    listed = files.list(str(home / "docs"))
    assert listed == {
        "folder": "docs",
        "entries": [
            {"name": "2025", "path": str(home / "docs" / "2025"), "kind": "folder"},
            {"name": "note.txt", "path": str(home / "docs" / "note.txt"), "kind": "file", "size": 23},
        ],
        "more": 0,
    }
    assert "is not a folder" in refused(files.list, str(home / "docs" / "note.txt"))
    assert files.touched == []


def test_a_listing_is_cut_and_says_how_many_more(home, monkeypatch):
    monkeypatch.setattr(filesmod, "LIST_MAX", 1)
    assert given(home).list(str(home / "docs"))["more"] == 1


def test_a_file_in_a_folder_given_to_read_is_read(home):
    files = given(home)
    read = files.read(str(home / "docs" / "2025" / "march.md"))
    assert (read.name, read.kind, read.text, read.truncated) == ("docs/2025/march.md", "text", read.text, False)
    assert read.text.startswith("# March") and read.size == files.read_bytes
    files.read(str(home / "docs" / "2025" / "march.md"))
    assert files.touched == [{"name": "docs/2025/march.md", "did": "read"}]
    assert "is not a file" in refused(files.read, str(home / "docs"))


def test_a_path_outside_the_folders_is_refused(home):
    files = given(home)
    said = refused(files.read, str(home / "private" / "secret.txt"))
    assert "is not in a folder this bot was given" in said and str(home / "docs") in said
    assert "'..'" in refused(files.read, str(home / "docs" / ".." / "private" / "secret.txt"))
    for bad in ("docs/note.txt", "/x\x00y", 7, None):
        assert "is not a path" in refused(files.read, bad)
    assert "it has none" in refused(Files([]).read, str(home / "docs" / "note.txt"))
    assert "is not in a folder" in refused(files.write, str(home / "private" / "new.txt"), "x")
    assert not (home / "private" / "new.txt").exists()


def test_a_link_is_no_way_out(home):
    files = given(home)
    (home / "docs" / "way-out").symlink_to(home / "private")
    (home / "docs" / "one.txt").symlink_to(home / "private" / "secret.txt")
    (home / "out" / "way-out").symlink_to(home / "private")
    (home / "out" / "dangling.txt").symlink_to(home / "private" / "made.txt")
    assert "is a link" in refused(files.read, str(home / "docs" / "way-out" / "secret.txt"))
    assert "is a link" in refused(files.read, str(home / "docs" / "one.txt"))
    assert "is a link" in refused(files.list, str(home / "docs" / "way-out"))
    assert "is a link" in refused(files.write, str(home / "out" / "way-out" / "new.txt"), "x")
    assert "is a link" in refused(files.write, str(home / "out" / "dangling.txt"), "x")
    assert not (home / "private" / "new.txt").exists() and not (home / "private" / "made.txt").exists()
    assert (home / "private" / "secret.txt").read_text() == "secret"


def test_a_folder_named_through_a_link_is_read_by_its_real_path(home, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(home / "docs")
    files = Files([{"path": str(alias), "access": "read"}])
    assert files.read(str(alias / "note.txt")).name == "alias/note.txt"
    assert files.read(str(home / "docs" / "note.txt")).text.startswith("alpha")


def test_a_write_in_a_folder_given_to_read_is_refused(home):
    files = given(home)
    assert "was given to read only" in refused(files.write, str(home / "docs" / "new.txt"), "x")
    assert "was given to read only" in refused(files.write, str(home / "out" / "kept" / "old.txt"), "x")
    assert "was given to read only" in refused(files.move, str(home / "docs" / "note.txt"), str(home / "out" / "n"))
    assert "was given to read only" in refused(files.move, str(home / "out" / "kept" / "old.txt"), str(home / "x"))
    assert not (home / "docs" / "new.txt").exists() and (home / "out" / "kept" / "old.txt").read_text() == "kept"
    assert files.touched == []


def test_a_file_is_written_in_a_folder_given_to_change(home):
    seen: list[Change] = []
    files = given(home, before=seen.append)
    made = files.write(str(home / "out" / "2025" / "Summary.md"), "# Summary\n")
    assert made == {"name": "out/2025/Summary.md", "bytes": 10, "created": True}
    assert (home / "out" / "2025" / "Summary.md").read_text() == "# Summary\n"
    again = files.write(str(home / "out" / "2025" / "Summary.md"), "v2")
    assert again["created"] is False and (home / "out" / "2025" / "Summary.md").read_text() == "v2"
    assert files.touched == [{"name": "out/2025/Summary.md", "did": "written"}]
    assert seen[0].view() == {
        "action": "write",
        "path": str(home / "out" / "2025" / "Summary.md"),
        "name": "out/2025/Summary.md",
    }
    assert "is a folder" in refused(files.write, str(home / "out" / "2025"), "x")
    assert "written as text" in refused(files.write, str(home / "out" / "b.bin"), b"x")
    assert "can be written" in refused(files.write, str(home / "out" / "big.txt"), "x" * (filesmod.WRITE_MAX + 1))


def test_a_change_the_program_refuses_is_not_made(home):
    def no(change: Change) -> None:
        raise FileRefused(f"The person said no to {change.name}.")

    files = given(home, before=no)
    assert "said no to out/new.txt" in refused(files.write, str(home / "out" / "new.txt"), "x")
    (home / "out" / "a.txt").write_text("a")
    assert "said no to out/a.txt" in refused(files.move, str(home / "out" / "a.txt"), str(home / "out" / "b.txt"))
    assert not (home / "out" / "new.txt").exists() and (home / "out" / "a.txt").exists()
    assert files.touched == []


def test_a_file_is_moved_or_renamed_within_folders_given_to_change(home):
    seen: list[Change] = []
    files = given(home, before=seen.append)
    (home / "out" / "a.txt").write_text("a")
    assert files.move(str(home / "out" / "a.txt"), str(home / "out" / "done" / "b.txt")) == {
        "name": "out/a.txt",
        "to": "out/done/b.txt",
    }
    assert (home / "out" / "done" / "b.txt").read_text() == "a" and not (home / "out" / "a.txt").exists()
    assert files.touched == [{"name": "out/done/b.txt", "did": "moved"}]
    assert seen[0].view()["to_name"] == "out/done/b.txt" and seen[0].view()["to"] == str(
        home / "out" / "done" / "b.txt"
    )
    (home / "out" / "c.txt").write_text("c")
    assert "is there already" in refused(files.move, str(home / "out" / "c.txt"), str(home / "out" / "done" / "b.txt"))
    assert "is not a file" in refused(files.move, str(home / "out" / "done"), str(home / "out" / "gone"))
    assert "is not in a folder" in refused(files.move, str(home / "out" / "c.txt"), str(home / "private" / "c.txt"))
    assert (home / "out" / "c.txt").read_text() == "c"


def test_a_word_document_is_read_as_its_text(home):
    doc = home / "docs" / "letter.docx"
    xml = (
        '<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Dear Ana,</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve">Fish &amp; chips</w:t><w:tab/><w:t>today</w:t><w:br/>'
        "<w:t>Yours</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml", xml)
    files = given(home)
    assert files.read(str(doc)).text == "Dear Ana,\nFish & chips\ttoday\nYours"
    (home / "docs" / "broken.docx").write_text("not a zip")
    assert "is not a Word document" in refused(files.read, str(home / "docs" / "broken.docx"))
    with zipfile.ZipFile(home / "docs" / "empty.docx", "w") as z:
        z.writestr("other.xml", "<x/>")
    assert "is not a Word document" in refused(files.read, str(home / "docs" / "empty.docx"))


def test_a_word_document_too_big_inside_is_refused(home, monkeypatch):
    with zipfile.ZipFile(home / "docs" / "big.docx", "w") as z:
        z.writestr("word/document.xml", "<w:p>" + "x" * 100 + "</w:p>")
    monkeypatch.setattr(filesmod, "FILE_MAX", 10)
    monkeypatch.setattr(os.path, "getsize", lambda _path: 1)
    assert "is too big to read" in refused(given(home).read, str(home / "docs" / "big.docx"))


def test_a_pdf_is_given_as_its_bytes_and_other_types_say_they_cannot_be_read(home):
    (home / "docs" / "march.pdf").write_bytes(b"%PDF-1.7 ...")
    (home / "docs" / "scan").write_bytes(b"%PDF-1.4 no suffix")
    (home / "docs" / "photo.png").write_bytes(b"\x89PNG\x00\x00")
    (home / "docs" / "latin.txt").write_bytes(b"caf\xe9")
    files = given(home)
    read = files.read(str(home / "docs" / "march.pdf"))
    assert (read.kind, read.data, read.text) == ("pdf", b"%PDF-1.7 ...", "")
    assert files.read(str(home / "docs" / "scan")).kind == "pdf"
    assert "cannot be read here" in refused(files.read, str(home / "docs" / "photo.png"))
    assert "cannot be read here" in refused(files.read, str(home / "docs" / "latin.txt"))
    assert [t["name"] for t in files.touched] == ["docs/march.pdf", "docs/scan"]


def test_reading_is_bounded_a_file_and_a_run(home, monkeypatch):
    files = given(home, read_max=30)
    files.read(str(home / "docs" / "note.txt"))
    assert "has read its limit of 30 bytes" in refused(files.read, str(home / "docs" / "2025" / "march.md"))
    monkeypatch.setattr(filesmod, "FILE_MAX", 5)
    assert "a file of at most 5 can be read" in refused(files.read, str(home / "docs" / "note.txt"))
    monkeypatch.setattr(filesmod, "FILE_MAX", 100)
    monkeypatch.setattr(filesmod, "TEXT_MAX", 5)
    cut = given(home).read(str(home / "docs" / "note.txt"))
    assert (cut.text, cut.truncated) == ("alpha", True)


def test_a_search_finds_names_and_lines(home):
    files = given(home)
    (home / "docs" / "Invoices.bin").write_bytes(b"\x00invoice")
    (home / "docs" / "way-out").symlink_to(home / "private")
    (home / "docs" / "one.txt").symlink_to(home / "private" / "secret.txt")
    march = home / "docs" / "2025" / "march.md"
    found = files.search(" INVOICE ")
    assert found == {
        "matches": [
            {"name": "docs/Invoices.bin", "path": str(home / "docs" / "Invoices.bin")},
            {"name": "docs/note.txt", "path": str(home / "docs" / "note.txt"), "line": 2, "text": "Invoice 7 is due"},
            {"name": "docs/2025/march.md", "path": str(march), "line": 2, "text": "the invoice was paid"},
        ],
        "more": 0,
    }
    assert files.search("march", str(home / "docs" / "2025"))["matches"] == [
        {"name": "docs/2025/march.md", "path": str(home / "docs" / "2025" / "march.md")},
        {"name": "docs/2025/march.md", "path": str(home / "docs" / "2025" / "march.md"), "line": 1, "text": "# March"},
    ]
    assert files.search("kept")["matches"] == [
        {"name": "kept/old.txt", "path": str(home / "out" / "kept" / "old.txt"), "line": 1, "text": "kept"}
    ]
    assert "needs some text" in refused(files.search, "  ")
    assert "needs some text" in refused(files.search, None)
    assert "is not in a folder" in refused(files.search, "x", str(home / "private"))
    assert files.touched == []


def test_a_search_is_bounded(home, monkeypatch):
    monkeypatch.setattr(filesmod, "FOUND_MAX", 1)
    assert given(home).search("invoice")["more"] == 1
    monkeypatch.setattr(filesmod, "SCAN_MAX", 3)
    assert given(home).search("invoice")["matches"] == []
    monkeypatch.setattr(filesmod, "WALK_MAX", 0)
    assert given(home).search("note")["matches"] == []


def test_a_file_gone_while_searching_is_passed_over(home, monkeypatch):
    def gone(_path: str) -> int:
        raise OSError("gone")

    monkeypatch.setattr(os.path, "getsize", gone)
    assert given(home).search("invoice")["matches"] == []


def test_with_nobody_to_ask_a_change_is_made(home):
    files = given(home)
    files.write(str(home / "out" / "plain.txt"), "x")
    assert (home / "out" / "plain.txt").read_text() == "x"
