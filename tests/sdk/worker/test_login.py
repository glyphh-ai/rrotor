"""A CLI's sign-in in this process (rrotor/_worker/login.py): sign-in scripts on a real pseudo-terminal, what they
print cleaned and read for an address and a code, answers typed or delivered, and the command stopped."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest

from rrotor._worker import login
from rrotor._worker.login import Session, clean, deliver, loopback, urls


def script(tmp_path: Path, body: str, name: str = "tool") -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def session(tmp_path: Path, body: str, quiet: float = 5.0, **sign_in: Any) -> Session:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    spec = {
        "run": "exec",
        "path": script(tmp_path, body),
        "args": [],
        "env": {"PATH": "/usr/bin:/bin", "HOME": str(home)},
        "cwd": str(tmp_path),
        "login": sign_in,
    }
    return Session(spec, out=io.BytesIO(), quiet=quiet, settle=(0.3, 1.0))


def test_clean_shows_what_a_terminal_would():
    raw = "\x1b[32mgreen\x1b[0m\r\n\x1b]8;;https://a.example.com/x\x07https://a.example.com/x\x1b]8;;\x07\r\r\n"
    raw += "\x1bPsome dcs\x1b\\a long line\x1b7\r⠋ wait\r⠙ wait\n" + "─" * 40 + "\x07\n"
    assert clean(raw) == "green\nhttps://a.example.com/x\n⠙ wait line\n───\n"


def test_urls_are_the_ones_to_open():
    text = "go to http://localhost:1455 then https://a.example.com/x?y=1. or (https://b.example.com/z) https://a.example.com/x?y=1"
    assert urls(text) == ["https://a.example.com/x?y=1", "https://b.example.com/z"]


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("http://localhost:1455/auth/callback?code=a&state=b", ("localhost", 1455, "/auth/callback?code=a&state=b")),
        (" http://127.0.0.1/cb ", ("127.0.0.1", 80, "/cb")),
        ("http://[::1]:9", ("::1", 9, "/")),
        ("https://localhost:1/x", None),
        ("http://example.com/x", None),
        ("http://localhost:99999/x", None),
        ("abc#def", None),
    ],
)
def test_a_pasted_loopback_redirect_is_recognised(text, found):
    assert loopback(text) == found


class Conn:
    def __init__(self, failing: set[str], host: str, port: int, timeout: float) -> None:
        self.failing, self.host = failing, host
        self.sent: tuple[str, str, dict[str, str]] | None = None
        self.closed = False

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        if self.host in self.failing:
            raise ConnectionRefusedError(f"refused on {self.host}")
        self.sent = (method, path, headers)

    def getresponse(self) -> Any:
        return type("Res", (), {"status": 200})()

    def close(self) -> None:
        self.closed = True


def test_deliver_tries_each_loopback_address_of_localhost():
    made: list[Conn] = []

    def connect(failing: set[str]) -> Any:
        def make(host: str, port: int, timeout: float) -> Conn:
            made.append(Conn(failing, host, port, timeout))
            return made[-1]

        return make

    assert deliver("localhost", 1455, "/cb?code=x", connect({"127.0.0.1"})) == 200
    assert [c.host for c in made] == ["127.0.0.1", "::1"] and all(c.closed for c in made)
    assert made[1].sent == ("GET", "/cb?code=x", {"Host": "localhost:1455"})
    with pytest.raises(ConnectionRefusedError, match=r"refused on 127\.0\.0\.1"):
        deliver("127.0.0.1", 1, "/", connect({"127.0.0.1"}))


def test_a_paste_flow_asks_then_types_the_answer_and_masks_it(tmp_path):
    body = (
        'echo "(node:5) [UNDICI-EHPA] Warning: experimental"\n'  # not a device code: it is before the address
        'echo\nprintf "Visit: https://auth.example.com/o?s=1\\r\\n"\nprintf "Paste code > "\nread code\n'
        'echo "$code" > "$HOME/creds"\necho "ok"\n'
    )
    s = session(tmp_path, body, signed_in="creds", home_files={"conf/a.json": "{}", "mine.json": "theirs"})
    (tmp_path / "home" / "mine.json").write_text("mine")
    assert s.start() is None
    assert (tmp_path / "home" / "conf" / "a.json").read_text() == "{}"
    assert (tmp_path / "home" / "mine.json").read_text() == "mine"  # never over what is there
    asked = s.until_asked()
    assert asked.status == "needs_input" and asked.data["url"] == "https://auth.example.com/o?s=1"
    assert asked.data["code"] is None and "the code the page shows" in str(asked.prompt)
    done = s.answer({"code": "secret-code-9"})
    assert done.status == "done" and done.data["signed_in"] is True, done.data
    assert "secret-code-9" not in done.data["output"]
    out = s.out
    assert isinstance(out, io.BytesIO) and b"secret-code-9" not in out.getvalue()
    s.stop()


def test_a_device_flow_does_not_type_the_answer(tmp_path):
    body = 'echo "visit https://dev.example.com/device and enter ABCD-EFGH"\nread typed\necho "typed: $typed"\n'
    s = session(tmp_path, body, quiet=0.6)
    s.start()
    asked = s.until_asked()
    assert (asked.data["code"], s.device) == ("ABCD-EFGH", True)
    again = s.answer("done")  # nothing typed: the CLI waits on, quiet, and is asked about again
    assert again.status == "needs_input" and again.data["url"] is None and s.device
    assert "still running and waiting" in str(again.prompt)
    assert s.answer("ok").status == "needs_input" and s.hidden == ["done"]  # too short to mask
    s.stop()
    assert s.proc is not None and s.proc.poll() is not None and s.master == -1
    s.stop()  # twice: nothing more to do


def test_a_polled_flow_that_takes_no_code(tmp_path):
    s = session(tmp_path, 'echo "open https://p.example.com/l"\nsleep 5\n', paste=False)
    s.start()
    asked = s.until_asked()
    assert asked.data["code"] is None and "any answer" in str(asked.prompt)
    s.stop()


def test_a_new_address_after_the_answer_is_asked_about(tmp_path):
    body = 'echo "https://one.example.com/a"\nread a\necho "now https://two.example.com/b"\nsleep 5\n'
    s = session(tmp_path, body, code=r"\d{6}")
    s.start()
    assert s.until_asked().data["url"] == "https://one.example.com/a"
    second = s.answer("first-answer")
    assert second.data["urls"] == ["https://two.example.com/b"]
    s.stop()


def test_a_spinner_never_lets_it_settle_so_it_asks_at_most_so_late(tmp_path):
    body = 'echo "https://spin.example.com/x"\nwhile true; do printf "\\r- waiting"; sleep 0.05; done\n'
    s = session(tmp_path, body)
    s.start()
    assert s.until_asked().data["url"] == "https://spin.example.com/x"
    assert "- waiting" in s.shown()
    s.stop()


def test_a_long_line_in_progress_is_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "LINE_MAX", 64)
    body = 'printf "%0200d"\nprintf "\\r%0100d"\nprintf "\\rhttps://cut.example.com/x"\nsleep 5\n'
    s = session(tmp_path, body)
    s.start()
    assert s.until_asked().data["url"] == "https://cut.example.com/x"
    assert len(s.raw) <= 200
    s.stop()


def test_a_redirect_is_delivered_or_its_failure_noted(tmp_path, monkeypatch):
    got: list[tuple[str, int, str]] = []
    monkeypatch.setattr(login, "deliver", lambda *a: got.append(a))
    s = session(tmp_path, 'echo "https://r.example.com/a"\nsleep 1\necho bye\n')
    s.start()
    s.until_asked()
    assert s.answer({"text": "http://localhost:1455/cb?code=z"}).status == "done"
    assert got == [("localhost", 1455, "/cb?code=z")]

    def refuse(*_: Any) -> None:
        raise ConnectionRefusedError("nobody listens")

    monkeypatch.setattr(login, "deliver", refuse)
    s = session(tmp_path, 'echo "https://r.example.com/a"\nsleep 5\n', quiet=0.5)
    s.start()
    s.until_asked()
    noted = s.answer("http://127.0.0.1:9/cb")
    assert "could not be opened inside the sandbox (nobody listens)" in noted.data["note"]
    assert str(noted.prompt).startswith("The address could not be opened")
    s.stop()


def test_a_command_signed_in_but_still_running_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "STOP_GRACE", 0.2)
    body = 'trap "" TERM\necho "https://g.example.com/a"\nread c\necho "$c" > "$HOME/creds"\n'
    body += "while true; do sleep 1; done\n"
    s = session(tmp_path, body, signed_in="creds")
    s.start()
    s.until_asked()
    done = s.answer("4/abc")
    assert (done.data["exit_code"], done.data["signed_in"]) == (None, True)
    assert s.proc is not None and s.proc.poll() is not None  # TERM ignored: killed


def test_a_command_that_cannot_start(tmp_path):
    s = session(tmp_path, "")
    s.spec["path"] = str(tmp_path / "missing")
    refused = s.start()
    assert refused is not None and refused.data["code"] == "E_CLI_EXEC"


def test_without_a_home_nothing_is_seeded_and_no_file_is_watched(tmp_path):
    s = session(tmp_path, "echo hi\n", signed_in="creds", home_files={"a": "b"})
    s.spec["env"].pop("HOME")
    s = Session(s.spec, out=io.BytesIO())
    assert s.home is None and s.signed_in is None
    s.start()
    assert s.until_asked().data == {"exit_code": 0, "signed_in": None, "output": "hi\n"}


def test_a_terminal_that_fails_to_read_is_its_end(tmp_path):
    s = session(tmp_path, "true\n")
    s.master = -1  # not a descriptor: the read fails, as Linux answers EIO once the command is gone
    s._read()
    assert s.eof


def test_a_signed_in_file_that_cannot_be_read_is_not_signed_in(tmp_path):
    s = session(tmp_path, "true\n", signed_in="creds")
    os.chmod(tmp_path / "home", 0o000)
    try:
        assert s._stamp() is None
    finally:
        os.chmod(tmp_path / "home", 0o700)
