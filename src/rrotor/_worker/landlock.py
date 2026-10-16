"""Landlock, through its three system calls (no library): the worker may read only the read-only mounts, write
only its folder and /tmp and, on ABI 4 and later, reach TCP only on the proxy's port. Applied before the model is
imported; a kernel without Landlock refuses the run (the runner checks it at start, too)."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# The same numbers on x86_64 and aarch64 (the unified syscall table).
SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1 << 0
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
PR_SET_NO_NEW_PRIVS = 38

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE, MAKE_CHAR, MAKE_DIR, MAKE_REG = 1 << 4, 1 << 5, 1 << 6, 1 << 7, 1 << 8
MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = 1 << 9, 1 << 10, 1 << 11, 1 << 12
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1

READ = EXECUTE | READ_FILE | READ_DIR
ALL_FS = {1: (1 << 13) - 1, 2: (1 << 14) - 1, 3: (1 << 15) - 1, 4: (1 << 15) - 1, 5: (1 << 16) - 1}


def fs_rights(abi: int) -> int:
    """Every filesystem right this ABI knows: each is then handled, so whatever no rule grants is refused."""
    return ALL_FS.get(abi, ALL_FS[5])


class RulesetAttr(ctypes.Structure):
    _fields_ = (("fs", ctypes.c_uint64), ("net", ctypes.c_uint64), ("scoped", ctypes.c_uint64))


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


class NetPort(ctypes.Structure):
    _fields_ = (("allowed", ctypes.c_uint64), ("port", ctypes.c_uint64))


class LandlockError(OSError):
    pass


Syscall = Callable[..., int]


def _libc_syscall() -> Syscall:  # pragma: no cover  (Linux only: exercised by the sandbox tests in the image)
    libc = ctypes.CDLL(None, use_errno=True)
    fn: Syscall = libc.syscall
    return fn


@dataclass(frozen=True, slots=True)
class Rules:
    read: tuple[str, ...]
    write: tuple[str, ...]
    tcp_ports: tuple[int, ...] = ()


class Landlock:
    def __init__(self, syscall: Syscall | None = None, prctl: Syscall | None = None) -> None:
        self._syscall = syscall or _libc_syscall()
        self._prctl = prctl or (lambda *a: ctypes.CDLL(None, use_errno=True).prctl(*a))  # pragma: no cover

    def _call(self, *args: object) -> int:
        result = self._syscall(*args)
        if result < 0:
            errno = ctypes.get_errno()
            raise LandlockError(errno, f"Landlock refused: {os.strerror(errno)}")
        return result

    def abi(self) -> int:
        """The kernel's Landlock ABI; 0 when it has none."""
        try:
            return self._call(SYS_CREATE_RULESET, None, 0, CREATE_RULESET_VERSION)
        except LandlockError:
            return 0

    def restrict(self, rules: Rules) -> int:
        """Lock this thread and every one it starts to `rules`. Answers the ABI used."""
        abi = self.abi()
        if abi < 1:
            raise LandlockError(0, "This kernel has no Landlock.")
        fs = fs_rights(abi)
        net = (NET_BIND_TCP | NET_CONNECT_TCP) if abi >= 4 else 0
        scoped = (SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL) if abi >= 6 else 0
        attr = RulesetAttr(fs, net, scoped)
        size = ctypes.sizeof(RulesetAttr) if abi >= 6 else (16 if abi >= 4 else 8)
        ruleset = self._call(SYS_CREATE_RULESET, ctypes.byref(attr), size, 0)
        try:
            self._paths(ruleset, rules.read, READ & fs)
            self._paths(ruleset, rules.write, fs)
            if net:
                for port in rules.tcp_ports:
                    rule = NetPort(net, port)
                    self._call(SYS_ADD_RULE, ruleset, RULE_NET_PORT, ctypes.byref(rule), 0)
            if self._prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
                raise LandlockError(ctypes.get_errno(), "no_new_privs could not be set")
            self._call(SYS_RESTRICT_SELF, ruleset, 0)
        finally:
            os.close(ruleset)
        return abi

    def _paths(self, ruleset: int, paths: Iterable[str], allowed: int) -> None:
        for path in paths:
            if not os.path.exists(path):
                continue
            fd = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
            try:
                rights = (
                    allowed
                    if os.path.isdir(path)
                    else allowed & (EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV)
                )
                rule = PathBeneath(rights, fd)
                self._call(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, ctypes.byref(rule), 0)
            finally:
                os.close(fd)
