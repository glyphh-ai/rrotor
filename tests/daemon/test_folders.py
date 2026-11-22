"""The machine's folders a run may be given (rrotord/folders.py): what RROTOR_FOLDERS may offer, what a call may ask
for, and what the platform's picker is told."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rrotord.errors import BadRequest, Forbidden, RrotorError
from rrotord.folders import MAX, Folders, Grant, roots_of


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A machine: Documents (offered) with invoices and a hidden folder in it, Private beside it, and rrotor's data."""
    root = Path(os.path.realpath(tmp_path)) / "home"
    for folder in ("Documents/invoices/2026", "Documents/.hidden", "Documents/letters", "Private", "data"):
        (root / folder).mkdir(parents=True)
    (root / "Documents" / "notes.txt").write_text("a file")
    (root / "Documents" / "way-out").symlink_to(root / "Private")
    (root / "Documents" / "shortcut").symlink_to(root / "Documents" / "letters")
    return root


def test_nothing_is_offered_until_the_owner_names_a_folder(home):
    assert roots_of("", home / "data") == () and roots_of(" : ", home / "data") == ()
    nothing = Folders()
    assert nothing.listing(None) == {"folders": []} and nothing.grants(None) == () and nothing.grants([]) == ()
    with pytest.raises(Forbidden) as refused:
        nothing.grants([{"path": str(home / "Documents"), "access": "read"}])
    assert refused.value.code == "E_FOLDER_REFUSED"
    assert refused.value.message == (
        f"{home}/Documents is not in a folder this rrotor's owner shares with bots: it shares none "
        "(RROTOR_FOLDERS is not set)."
    )


def test_the_roots_are_the_real_folders_named_once(home):
    text = f"{home}/Documents: {home}/Private/ :{home}/Documents/shortcut:{home}/Documents"
    assert roots_of(text, home / "data") == (home / "Documents", home / "Private", home / "Documents" / "letters")


@pytest.mark.parametrize("bad", ["Documents", "/a/../b", "/", "{home}", "{home}/data", "{home}/data/x", "/usr/lib"])
def test_a_root_that_cannot_be_offered_stops_rrotor(home, bad):
    with pytest.raises(RrotorError) as refused:
        roots_of(bad.format(home=home), home / "data")
    assert refused.value.code == "E_CONFIG" and "RROTOR_FOLDERS" in refused.value.message


def test_a_call_is_given_the_folders_it_asks_for_inside_the_roots(home):
    folders = Folders((home / "Documents",))
    docs = str(home / "Documents")
    asked = [
        {"path": f"{docs}/invoices/2026", "access": "read"},
        {"path": docs, "access": "write"},
        {"path": f"{docs}/invoices/2026/", "access": "write"},  # the same folder twice: the wider say holds
        {"path": f"{docs}/letters", "access": "read"},
    ]
    given = folders.grants(asked)
    assert given == (
        Grant(docs, True),
        Grant(f"{docs}/invoices/2026", True),
        Grant(f"{docs}/letters", False),
    )
    assert given[2].view() == {"path": f"{docs}/letters", "access": "read"}
    assert given[0].view() == {"path": docs, "access": "write"}


def test_a_folder_outside_the_roots_is_refused_in_a_plain_sentence(home):
    folders = Folders((home / "Documents",))
    docs = home / "Documents"
    outside = f"is not in a folder this rrotor's owner shares with bots: it shares {docs}."
    for path, why in (
        (str(home / "Private"), outside),
        (str(home), outside),
        ("/etc", outside),
        (f"{docs}/../Private", outside),
        (f"{docs}/invoices/../../Private", outside),
        (f"{docs}/way-out", outside),  # a link out of the root is no way in
        (f"{docs}/missing", "is not a folder on this rrotor."),
        (f"{docs}/notes.txt", "is not a folder on this rrotor."),
        (f"{docs}/shortcut", f"is a link to {docs}/letters: name that folder itself."),
    ):
        with pytest.raises(Forbidden) as refused:
            folders.grants([{"path": path, "access": "read"}])
        assert refused.value.code == "E_FOLDER_REFUSED" and refused.value.message == f"{path} {why}", path


def test_a_call_whose_folders_are_not_folders_is_refused(home):
    folders = Folders((home / "Documents",))
    docs = str(home / "Documents")
    for asked in (
        "all",
        {"path": docs},
        [docs],
        [{"path": docs}],
        [{"path": docs, "access": "own"}],
        [{"path": "Documents", "access": "read"}],
        [{"path": 7, "access": "read"}],
        [{"path": docs + "\x00", "access": "read"}],
        [{"path": docs, "access": "read"}] * (MAX + 1),
    ):
        with pytest.raises(BadRequest) as refused:
            folders.grants(asked)
        assert refused.value.code == "E_FOLDER", asked
    assert len(folders.grants([{"path": docs, "access": "read"}] * MAX)) == 1


def test_the_picker_lists_the_roots_then_the_folders_inside_one(home):
    docs = home / "Documents"
    folders = Folders((docs, home / "gone", Path("/")))
    assert folders.listing(None) == {"folders": [{"path": str(docs), "name": "Documents"}, {"path": "/", "name": "/"}]}
    folders = Folders((docs,))
    assert folders.listing(str(docs)) == {  # folders only: no file, no hidden folder, no link
        "folders": [{"path": f"{docs}/invoices", "name": "invoices"}, {"path": f"{docs}/letters", "name": "letters"}]
    }
    assert folders.listing(f"{docs}/invoices/") == {"folders": [{"path": f"{docs}/invoices/2026", "name": "2026"}]}
    assert folders.listing(f"{docs}/letters") == {"folders": []}


def test_the_picker_stays_inside_the_roots(home, monkeypatch):
    docs = home / "Documents"
    folders = Folders((docs,))
    for under in (str(home), str(home / "Private"), f"{docs}/..", f"{docs}/invoices/../..", f"{docs}/way-out", "/"):
        with pytest.raises(Forbidden) as refused:
            folders.listing(under)
        assert refused.value.code == "E_FOLDER_REFUSED", under
    for under in ("", "Documents", "~/Documents"):
        with pytest.raises(BadRequest):
            folders.listing(under)

    def unreadable(path: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "scandir", unreadable)
    with pytest.raises(Forbidden) as refused:
        folders.listing(str(docs))
    assert refused.value.message == f"rrotor cannot read {docs}."


def test_a_long_listing_is_cut(home, monkeypatch):
    import rrotord.folders as module

    monkeypatch.setattr(module, "LISTED_MAX", 1)
    assert Folders((home / "Documents",)).listing(str(home / "Documents")) == {
        "folders": [{"path": f"{home}/Documents/invoices", "name": "invoices"}]
    }
