import os

from rrotord.proc import Done
from rrotord.sandbox.probe import Layers, probe


class Landlock:
    def __init__(self, abi: int | Exception) -> None:
        self._abi = abi

    def abi(self) -> int:
        if isinstance(self._abi, Exception):
            raise self._abi
        return self._abi


class Seccomp:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def build(self) -> int:
        if self.fail:
            raise OSError("no libseccomp")
        return os.open(os.devnull, os.O_RDONLY)


def runner(code: int):
    calls = []

    async def run(argv, **kw):
        calls.append(argv)
        return Done(code, "", "denied")

    run.calls = calls  # type: ignore[attr-defined]
    return run


async def test_every_layer_ready():
    run = runner(0)
    layers = await probe(Landlock(6), Seccomp(), run, lambda name: f"/usr/bin/{name}")  # type: ignore[arg-type]
    assert layers.ready and layers.why() == ""
    assert run.calls[0][0] == "/usr/bin/bwrap"
    assert layers.to_json()["layers"]["landlock"] == {"ok": True, "detail": "ABI 6"}


async def test_every_layer_missing_says_why():
    layers = await probe(Landlock(0), Seccomp(fail=True), runner(0), lambda name: None)  # type: ignore[arg-type]
    assert not layers.ready
    why = layers.why()
    assert "bubblewrap: bwrap is not installed" in why and "no Landlock" in why
    assert "libseccomp could not build the filter: no libseccomp" in why and "prlimit (util-linux)" in why


async def test_bwrap_that_cannot_make_namespaces():
    layers = await probe(Landlock(1), Seccomp(), runner(1), lambda name: name)  # type: ignore[arg-type]
    assert "cannot make its namespaces here (denied)" in layers.checks["bubblewrap"]["detail"]


async def test_landlock_that_errors_says_its_error():
    layers = await probe(Landlock(OSError("ENOSYS")), Seccomp(), runner(0), lambda name: name)  # type: ignore[arg-type]
    assert layers.checks["landlock"] == {"ok": False, "detail": "ENOSYS"}


def test_no_checks_is_not_ready():
    assert not Layers().ready
