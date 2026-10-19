"""Shared fixtures: the licence's public key patched to the tests' own (and its /etc file away from the host's), short
folders for unix sockets, git repos and bundles of sample models, restored signals."""

from __future__ import annotations

import shutil
import signal
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from rrotord import licence, licence_key
from tests.fakes import GitRepo, keys


@pytest.fixture(autouse=True)
def licence_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Licence keys in tests are signed by the tests' own pair: the module's tuple patched, as the spec says."""
    monkeypatch.setattr(licence_key, "PUBLIC_KEYS", (keys().public_url,))
    monkeypatch.setattr(licence, "ETC_KEY", Path("/nonexistent/rrotor/license.key"))  # never the host's own


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A folder with a short path: a unix socket's path must fit in 104 bytes on macOS."""
    path = Path(tempfile.mkdtemp(prefix="rr", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path / "repo")


@pytest.fixture
def restore_signals() -> Iterator[None]:
    """The worker installs handlers for SIGALRM and SIGUSR1 in this process: put them back."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGUSR1)}
    yield
    signal.setitimer(signal.ITIMER_REAL, 0)
    for s, handler in saved.items():
        signal.signal(s, handler)
