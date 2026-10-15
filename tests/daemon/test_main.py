import logging
from collections.abc import Callable

from rrotord import __main__ as entry
from rrotord import daemon


def keep_logging(fn: Callable[[], int]) -> int:
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        return fn()
    finally:
        root.handlers[:] = saved
        root.setLevel(level)


def test_bad_settings_exit_2_saying_why(monkeypatch, capsys):
    monkeypatch.setenv("RROTOR_MODE", "nope")
    assert keep_logging(entry.main) == 2
    assert "rrotord: E_CONFIG: RROTOR_MODE must be local or hosted." in capsys.readouterr().err


def test_good_settings_start_the_daemon(monkeypatch):
    served = []

    async def serve(settings):
        served.append(settings)

    monkeypatch.setenv("RROTOR_MODE", "hosted")
    monkeypatch.setenv("RROTOR_SERVER", "http://s")
    monkeypatch.setattr(daemon, "main", serve)
    assert keep_logging(entry.main) == 0
    assert served[0].mode == "hosted"
