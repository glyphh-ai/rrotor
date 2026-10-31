import ctypes
import os

import pytest

from rrotor._worker import landlock as ll
from rrotor._worker.landlock import Landlock, LandlockError, Rules


class Kernel:
    """Landlock's three system calls, as a kernel of a given ABI answers them."""

    def __init__(self, abi: int, fail_on: int | None = None) -> None:
        self.abi, self.fail_on = abi, fail_on
        self.calls: list[tuple[object, ...]] = []
        self.rulesets: list[int] = []

    def __call__(self, nr: int, *args: object) -> int:
        self.calls.append((nr, *args))
        if nr == self.fail_on or (nr == ll.SYS_CREATE_RULESET and self.abi == 0):
            ctypes.set_errno(1)
            return -1
        if nr == ll.SYS_CREATE_RULESET:
            if args[2] == ll.CREATE_RULESET_VERSION:
                return self.abi
            fd = os.open(os.devnull, os.O_RDONLY)
            self.rulesets.append(fd)
            return fd
        return 0


def ok_prctl(*_: object) -> int:
    return 0


def test_fs_rights_per_abi():
    assert ll.fs_rights(1) == (1 << 13) - 1
    assert ll.fs_rights(9) == (1 << 16) - 1


def test_abi_is_the_kernels():
    assert Landlock(Kernel(3), ok_prctl).abi() == 3


def test_abi_is_zero_without_landlock():
    assert Landlock(Kernel(0), ok_prctl).abi() == 0


def test_restrict_refuses_a_kernel_without_landlock():
    with pytest.raises(LandlockError, match="no Landlock"):
        Landlock(Kernel(0), ok_prctl).restrict(Rules((), ()))


def test_restrict_adds_paths_and_ports_on_abi_6(tmp_path):
    folder, file = tmp_path / "d", tmp_path / "f"
    folder.mkdir()
    file.write_text("x")
    kernel = Kernel(6)
    abi = Landlock(kernel, ok_prctl).restrict(
        Rules((str(folder), str(file), str(tmp_path / "gone")), (str(folder),), (3128,))
    )
    assert abi == 6
    adds = [c for c in kernel.calls if c[0] == ll.SYS_ADD_RULE]
    assert [c[2] for c in adds] == [ll.RULE_PATH_BENEATH] * 3 + [ll.RULE_NET_PORT]
    assert kernel.calls[-1][0] == ll.SYS_RESTRICT_SELF
    create = [c for c in kernel.calls if c[0] == ll.SYS_CREATE_RULESET][1]
    assert create[2] == ctypes.sizeof(ll.RulesetAttr)


def test_restrict_on_abi_4_handles_tcp_but_not_scopes():
    kernel = Kernel(4)
    Landlock(kernel, ok_prctl).restrict(Rules((), (), (3128,)))
    create = [c for c in kernel.calls if c[0] == ll.SYS_CREATE_RULESET][1]
    assert create[2] == 16
    assert any(c[2] == ll.RULE_NET_PORT for c in kernel.calls if c[0] == ll.SYS_ADD_RULE)


def test_restrict_on_abi_1_has_no_network_rules():
    kernel = Kernel(1)
    Landlock(kernel, ok_prctl).restrict(Rules((), (), (3128,)))
    create = [c for c in kernel.calls if c[0] == ll.SYS_CREATE_RULESET][1]
    assert create[2] == 8
    assert not [c for c in kernel.calls if c[0] == ll.SYS_ADD_RULE]


def test_restrict_fails_when_no_new_privs_cannot_be_set():
    with pytest.raises(LandlockError, match="no_new_privs"):
        Landlock(Kernel(2), lambda *_: -1).restrict(Rules((), ()))


def test_restrict_fails_when_a_rule_is_refused(tmp_path):
    kernel = Kernel(2, fail_on=ll.SYS_ADD_RULE)
    with pytest.raises(LandlockError, match="Landlock refused"):
        Landlock(kernel, ok_prctl).restrict(Rules((str(tmp_path),), ()))


def test_a_clis_run_executes_nothing_it_may_write(tmp_path):
    """A CLI's run: its writable folders lose the right to execute; a program's keep every right."""
    allowed = []
    for exec_writes in (True, False):
        kernel = Kernel(6)
        Landlock(kernel, ok_prctl).restrict(Rules((), (str(tmp_path),), (), exec_writes))
        add = next(c for c in kernel.calls if c[0] == ll.SYS_ADD_RULE)
        allowed.append(add[3]._obj.allowed)  # type: ignore[attr-defined]
    assert allowed[0] & ll.EXECUTE and not allowed[1] & ll.EXECUTE
    assert allowed[0] & ~ll.EXECUTE == allowed[1]
