import errno
import os
import tempfile

import pytest

from rrotord.sandbox import seccomp
from rrotord.sandbox.seccomp import Filter, SeccompError


class Lib:
    """libseccomp, as far as the filter uses it."""

    def __init__(
        self, ctx: int = 1, rule: int = 0, export: int = 0, unknown: frozenset[str] = frozenset({"iopl"})
    ) -> None:
        self.ctx, self.rule_rc, self.export_rc, self.unknown = ctx, rule, export, unknown
        self.rules: list[tuple[int, int, int, object]] = []
        self.released = False

    def seccomp_init(self, action: int) -> int:
        assert action == seccomp.SCMP_ACT_ALLOW
        return self.ctx

    def seccomp_syscall_resolve_name(self, name: bytes) -> int:
        return -1 if name.decode() in self.unknown else len(self.rules) + 1

    def seccomp_rule_add_array(self, ctx: int, action: int, nr: int, count: int, array: object) -> int:
        self.rules.append((action, nr, count, array))
        return self.rule_rc

    def seccomp_export_bpf(self, ctx: int, fd: int) -> int:
        if self.export_rc == 0:
            os.write(fd, b"BPF")
        return self.export_rc

    def seccomp_release(self, ctx: int) -> None:
        self.released = True


def memfd(name: str) -> int:
    fd, path = tempfile.mkstemp()
    os.unlink(path)
    return fd


def test_act_errno():
    assert seccomp.act_errno(errno.EPERM) == 0x00050001


def test_build_exports_the_filter_from_the_start():
    lib = Lib()
    fd = Filter(lib, memfd).build()
    try:
        assert os.read(fd, 10) == b"BPF"
    finally:
        os.close(fd)
    assert lib.released
    actions = [r[0] for r in lib.rules]
    assert actions.count(seccomp.act_errno(errno.EAFNOSUPPORT)) == 9
    assert seccomp.act_errno(errno.ENOSYS) in actions
    assert len(lib.rules) == len(seccomp.DENIED) - 1 + 1 + 1 + 9  # iopl is unknown here
    with_args = [r for r in lib.rules if r[2]]
    assert all(r[3] is not None for r in with_args) and len(with_args) == 10


def test_build_fails_when_init_fails():
    with pytest.raises(SeccompError, match="seccomp_init"):
        Filter(Lib(ctx=0), memfd).build()


def test_build_fails_when_a_rule_is_refused():
    lib = Lib(rule=-1)
    with pytest.raises(SeccompError, match="refused the rule for ptrace"):
        Filter(lib, memfd).build()
    assert lib.released


def test_build_fails_when_the_export_fails():
    with pytest.raises(SeccompError, match="export"):
        Filter(Lib(export=-1), memfd).build()
