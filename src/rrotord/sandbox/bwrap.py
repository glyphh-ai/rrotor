"""The `bwrap` command line for one run: new namespaces, a read-only system, the version's code and venv, its folder
writable, the SDK, the proxy's socket and nothing else of /data. On a cluster's follower (`readonly`) the folder and
the data folder are mounted read-only too: the head is the only writer of what syncs (docs/specs/cluster.md, 3). A
CLI's run adds its install (read-only), its per-call working folder and, when it keeps one, its private HOME."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROGRAM, VENV, SDK, FILES, DATA, PARTS = "/program", "/venv", "/opt/rrotor", "/files", "/data", "/opt/parts"
CLI, WORK, CLI_HOME = "/cli", "/work", "/home/cli"
PROXY_SOCKET = "/run/rrotor/proxy.sock"
SYSTEM_RO = ("/usr", "/etc/ssl", "/etc/ca-certificates", "/etc/localtime", "/etc/alternatives")
LINKS = (("usr/bin", "/bin"), ("usr/sbin", "/sbin"), ("usr/lib", "/lib"), ("usr/lib64", "/lib64"))


@dataclass(frozen=True, slots=True)
class Mounts:
    src: Path
    venv: Path
    sdk: Path  # the rrotor package's folder
    files: Path
    proxy: Path | None
    data: Path | None = None  # the program's persistent data folder, when its manifest asks
    parts: tuple[tuple[str, Path], ...] = ()  # the parts its manifest asks for: (name, where it is installed)
    pythonpath: tuple[str, ...] = ()  # the parts' PYTHONPATH entries, as paths inside the sandbox
    readonly: bool = False  # a follower's run: /files and /data read-only
    cli: Path | None = None  # a CLI's run: its version's install, read-only at /cli
    work: Path | None = None  # its per-call working folder, at /work
    home: Path | None = None  # its private persistent HOME ("home": true), at /home/cli
    listen: bool = False  # a CLI's sign-in that listens on loopback: TCP left to the sandbox's own network


@dataclass(frozen=True, slots=True)
class Limits:
    memory_mb: int
    cpu_seconds: int
    open_files: int = 256
    file_mb: int = 1024


def landlock_rules(mounts: Mounts) -> dict[str, object]:
    """What the worker locks itself to, as paths inside the sandbox. A CLI's run (and every process it starts)
    executes nothing it may write: only the system, its install and the version's venv. A CLI's sign-in that listens
    for the browser's redirect (`listen`) may bind and connect any TCP port: the sandbox's network namespace has only
    its own loopback (and the proxy's forwarder on it), so nothing outside can reach the listener or be reached."""
    mine = [FILES] + ([DATA] if mounts.data else []) + ([CLI_HOME] if mounts.home else [])
    write = [FILES, "/tmp", "/dev", *mine[1:]] if not mounts.readonly else ["/tmp", "/dev"]
    read = [*SYSTEM_RO, PROGRAM, VENV, SDK, "/proc", *(f"{PARTS}/{name}" for name, _ in mounts.parts)]
    return {
        "read": read + ([CLI] if mounts.cli else []) + (mine if mounts.readonly else []),
        "write": write + ([WORK] if mounts.work else []),
        "tcp_ports": [3128] if mounts.proxy else [],
        "exec_writes": mounts.cli is None,
        "net": not mounts.listen,
    }


def prlimit(limits: Limits) -> list[str]:
    mb = 1024 * 1024
    return [
        "prlimit",
        f"--data={limits.memory_mb * mb}",
        f"--cpu={limits.cpu_seconds}",
        f"--nofile={limits.open_files}",
        f"--fsize={limits.file_mb * mb}",
        "--core=0",
        "--",
    ]


def command(
    mounts: Mounts, limits: Limits, seccomp_fd: int, channel_fd: int, exists: object = Path.exists
) -> list[str]:
    argv = [
        *prlimit(limits),
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--uid",
        "1000",
        "--gid",
        "1000",
        "--hostname",
        "rrotor",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/run/rrotor",
    ]
    for path in SYSTEM_RO:
        if exists(Path(path)):  # type: ignore[operator]
            argv += ["--ro-bind", path, path]
    for target, link in LINKS:
        if exists(Path("/") / target):  # type: ignore[operator]
            argv += ["--symlink", target, link]
    argv += [
        "--ro-bind",
        str(mounts.src),
        PROGRAM,
        "--ro-bind",
        str(mounts.venv),
        VENV,
        "--ro-bind",
        str(mounts.sdk),
        f"{SDK}/rrotor",
        "--ro-bind" if mounts.readonly else "--bind",
        str(mounts.files),
        FILES,
    ]
    if mounts.data:
        argv += ["--ro-bind" if mounts.readonly else "--bind", str(mounts.data), DATA]
    for name, where in mounts.parts:
        argv += ["--ro-bind", str(where), f"{PARTS}/{name}"]
    if mounts.cli:
        argv += ["--ro-bind", str(mounts.cli), CLI]
    if mounts.work:
        argv += ["--bind", str(mounts.work), WORK]
    if mounts.home:
        argv += ["--ro-bind" if mounts.readonly else "--bind", str(mounts.home), CLI_HOME]
    if mounts.proxy:
        argv += ["--bind", str(mounts.proxy), PROXY_SOCKET]
    env = {
        "PATH": f"{VENV}/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "PYTHONPATH": ":".join([SDK, *mounts.pythonpath]),
        "PYTHONDONTWRITEBYTECODE": "1",
        # numpy's BLAS on one thread: its per-thread buffers would not fit the data limit on a many-core host
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "MALLOC_ARENA_MAX": "2",
        "RROTOR_CHANNEL_FD": str(channel_fd),
    }
    if mounts.proxy:
        env |= {
            "HTTPS_PROXY": "http://127.0.0.1:3128",
            "https_proxy": "http://127.0.0.1:3128",
            "HTTP_PROXY": "http://127.0.0.1:3128",
            "http_proxy": "http://127.0.0.1:3128",
        }
    for key, value in env.items():
        argv += ["--setenv", key, value]
    argv += ["--seccomp", str(seccomp_fd), "--chdir", PROGRAM, "--", f"{VENV}/bin/python", "-m", "rrotor._worker"]
    return argv
