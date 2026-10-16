"""The `bwrap` command line for one run: new namespaces, a read-only system, the version's code and venv, its folder
writable, the SDK, the proxy's socket and nothing else of /data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MODEL, VENV, SDK, FILES, DATA, PARTS = "/model", "/venv", "/opt/rrotor", "/files", "/data", "/opt/parts"
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
    data: Path | None = None  # the model's persistent data folder, when its manifest asks
    parts: tuple[tuple[str, Path], ...] = ()  # the parts its manifest asks for: (name, where it is installed)
    pythonpath: tuple[str, ...] = ()  # the parts' PYTHONPATH entries, as paths inside the sandbox


@dataclass(frozen=True, slots=True)
class Limits:
    memory_mb: int
    cpu_seconds: int
    open_files: int = 256
    file_mb: int = 1024


def landlock_rules(mounts: Mounts) -> dict[str, object]:
    """What the worker locks itself to, as paths inside the sandbox."""
    write = [FILES, "/tmp", "/dev"] + ([DATA] if mounts.data else [])
    return {
        "read": [*SYSTEM_RO, MODEL, VENV, SDK, "/proc", *(f"{PARTS}/{name}" for name, _ in mounts.parts)],
        "write": write,
        "tcp_ports": [3128] if mounts.proxy else [],
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
        MODEL,
        "--ro-bind",
        str(mounts.venv),
        VENV,
        "--ro-bind",
        str(mounts.sdk),
        f"{SDK}/rrotor",
        "--bind",
        str(mounts.files),
        FILES,
    ]
    if mounts.data:
        argv += ["--bind", str(mounts.data), DATA]
    for name, where in mounts.parts:
        argv += ["--ro-bind", str(where), f"{PARTS}/{name}"]
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
    argv += ["--seccomp", str(seccomp_fd), "--chdir", MODEL, "--", f"{VENV}/bin/python", "-m", "rrotor._worker"]
    return argv
