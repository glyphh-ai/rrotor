"""The seccomp filter every run gets, built with libseccomp (through ctypes, no Python binding) and handed to
`bwrap --seccomp <fd>` as BPF. Allow by default, refuse what a model never needs and what could break out:
namespaces, mounts, tracing, kernel modules, BPF, io_uring, keyrings, clocks; new user namespaces through clone;
sockets other than unix, IPv4 and IPv6 (no netlink, no raw packets). clone3 answers ENOSYS so the C library falls
back to clone, whose flags the filter can read."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from collections.abc import Callable
from typing import Any

SCMP_ACT_ALLOW = 0x7FFF0000
CLONE_NEWUSER = 0x10000000
AF_UNIX, AF_INET, AF_INET6 = 1, 2, 10
SCMP_CMP_EQ, SCMP_CMP_GE, SCMP_CMP_MASKED_EQ = 4, 5, 7


def act_errno(code: int) -> int:
    return 0x00050000 | (code & 0xFFFF)


DENIED: tuple[str, ...] = (
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "unshare",
    "setns",
    "move_mount",
    "open_tree",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "mount_setattr",
    "kexec_load",
    "kexec_file_load",
    "init_module",
    "finit_module",
    "delete_module",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "keyctl",
    "add_key",
    "request_key",
    "acct",
    "swapon",
    "swapoff",
    "reboot",
    "syslog",
    "settimeofday",
    "clock_settime",
    "clock_adjtime",
    "adjtimex",
    "quotactl",
    "open_by_handle_at",
    "name_to_handle_at",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "lookup_dcookie",
    "personality",
    "uselib",
    "vhangup",
    "iopl",
    "ioperm",
    "nfsservctl",
    "fanotify_init",
    "pidfd_getfd",
)


class ArgCmp(ctypes.Structure):
    _fields_ = (
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    )


class SeccompError(OSError):
    pass


def _load() -> Any:  # pragma: no cover  (libseccomp is in the image; the sandbox tests run it there)
    name = ctypes.util.find_library("seccomp") or "libseccomp.so.2"
    lib = ctypes.CDLL(name, use_errno=True)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ArgCmp),
    ]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    return lib


class Filter:
    def __init__(self, lib: Any | None = None, memfd: Callable[[str], int] | None = None) -> None:
        self._lib = lib if lib is not None else _load()
        self._memfd = memfd or (lambda name: os.memfd_create(name, 0))  # pragma: no cover  (Linux)

    def _rule(self, ctx: int, action: int, name: str, *cmps: ArgCmp) -> None:
        nr = self._lib.seccomp_syscall_resolve_name(name.encode())
        if nr < 0:
            return  # not a system call on this architecture
        array = (ArgCmp * len(cmps))(*cmps) if cmps else None
        if self._lib.seccomp_rule_add_array(ctx, action, nr, len(cmps), array) < 0:
            raise SeccompError(errno.EINVAL, f"seccomp refused the rule for {name}")

    def build(self) -> int:
        """The filter as BPF in a sealed memory file: its descriptor, for `bwrap --seccomp`."""
        ctx = self._lib.seccomp_init(SCMP_ACT_ALLOW)
        if not ctx:
            raise SeccompError(errno.ENOMEM, "seccomp_init failed")
        try:
            for name in DENIED:
                self._rule(ctx, act_errno(errno.EPERM), name)
            self._rule(ctx, act_errno(errno.ENOSYS), "clone3")
            self._rule(
                ctx, act_errno(errno.EPERM), "clone", ArgCmp(0, SCMP_CMP_MASKED_EQ, CLONE_NEWUSER, CLONE_NEWUSER)
            )
            for family in (0, 3, 4, 5, 6, 7, 8, 9):
                self._rule(ctx, act_errno(errno.EAFNOSUPPORT), "socket", ArgCmp(0, SCMP_CMP_EQ, family, 0))
            self._rule(ctx, act_errno(errno.EAFNOSUPPORT), "socket", ArgCmp(0, SCMP_CMP_GE, AF_INET6 + 1, 0))
            fd = self._memfd("rrotor-seccomp")
            if self._lib.seccomp_export_bpf(ctx, fd) < 0:
                os.close(fd)
                raise SeccompError(errno.EIO, "seccomp could not export its filter")
            os.lseek(fd, 0, os.SEEK_SET)
            return fd
        finally:
            self._lib.seccomp_release(ctx)
