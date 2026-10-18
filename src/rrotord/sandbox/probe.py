"""Every sandbox layer checked at start; a layer that cannot work makes rrotor refuse runs, saying which and why.
Nothing is skipped silently (docs/architecture.md, section 4). When bwrap cannot start, what the container lacks is
read from /proc and said plainly (a self-hosted rrotor's health carries it)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rrotor._worker.landlock import Landlock
from rrotord.proc import Done, run
from rrotord.sandbox.bwrap import LINKS
from rrotord.sandbox.seccomp import Filter

Runner = Callable[..., Awaitable[Done]]

# What bwrap needs of the container, and how to give it.
NEEDS = {
    "seccomp": "seccomp unconfined: the container runs under a seccomp profile, which stops bwrap making its "
    "namespaces (Docker: --security-opt seccomp=unconfined; Kubernetes: seccompProfile type Unconfined)",
    "apparmor": "AppArmor unconfined: the container runs under an AppArmor profile, which stops bwrap mounting "
    "(Docker: --security-opt apparmor=unconfined; Kubernetes: appArmorProfile type Unconfined)",
    "proc": "/proc unmasked: parts of /proc are masked or read-only, so bwrap cannot mount a /proc of its own "
    "(Docker: --security-opt systempaths=unconfined; Kubernetes: procMount Unmasked)",
    "userns": "unprivileged user namespaces: the host restricts them through AppArmor (Ubuntu 23.10 and later), so "
    "bwrap cannot bring up its network; on the host: sysctl -w kernel.apparmor_restrict_unprivileged_userns=0",
}


def read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def container_needs(reader: Callable[[Path], str] = read) -> list[str]:
    """What this container lacks for bwrap, as /proc shows it; every need when none of them shows."""
    status = reader(Path("/proc/self/status"))
    profile = reader(Path("/proc/self/attr/current")).strip().rstrip("\x00").strip()
    mounts = [line.split()[1] for line in reader(Path("/proc/self/mounts")).splitlines() if len(line.split()) > 1]
    lacks = {
        "seccomp": any(line.split() == ["Seccomp:", "2"] for line in status.splitlines()),
        "apparmor": profile not in ("", "unconfined"),
        "proc": any(m.startswith("/proc/") for m in mounts),
        "userns": reader(Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")).strip() == "1",
    }
    found = [NEEDS[k] for k, lacking in lacks.items() if lacking]
    return found or list(NEEDS.values())


@dataclass(slots=True)
class Layers:
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)  # what the container lacks for bwrap, when it cannot start

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(c["ok"] for c in self.checks.values())

    def why(self) -> str:
        return "; ".join(f"{k}: {c['detail']}" for k, c in self.checks.items() if not c["ok"])

    def to_json(self) -> dict[str, Any]:
        if self.needs:
            return {"ready": self.ready, "layers": self.checks, "container_needs": self.needs}
        return {"ready": self.ready, "layers": self.checks}


async def probe(
    landlock: Landlock | None = None,
    seccomp: Filter | None = None,
    runner: Runner = run,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[Path], bool] = Path.exists,
) -> Layers:
    layers = Layers()

    bwrap = which("bwrap")
    if bwrap is None:
        layers.checks["bubblewrap"] = {"ok": False, "detail": "bwrap is not installed"}
    else:
        links = [arg for target, link in LINKS if exists(Path("/") / target) for arg in ("--symlink", target, link)]
        done = await runner(
            [
                bwrap,
                "--unshare-all",
                "--die-with-parent",
                "--ro-bind",
                "/usr",
                "/usr",
                *links,
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--",
                "/usr/bin/true",
            ],
            timeout=20,
        )
        layers.checks["bubblewrap"] = (
            {"ok": True, "detail": "namespaces: user, mount, pid, ipc, uts, cgroup, net"}
            if done.code == 0
            else {
                "ok": False,
                "detail": f"bwrap cannot make its namespaces here ({done.why(300)}); a container needs "
                "--security-opt seccomp=unconfined --security-opt systempaths=unconfined "
                "--security-opt apparmor=unconfined",
            }
        )

    try:
        abi = (landlock or Landlock()).abi()
    except OSError as err:
        abi, error = 0, str(err)
    else:
        error = "this kernel has no Landlock (or it is turned off)"
    layers.checks["landlock"] = {"ok": True, "detail": f"ABI {abi}"} if abi >= 1 else {"ok": False, "detail": error}

    try:
        fd = (seccomp or Filter()).build()
        os.close(fd)
        layers.checks["seccomp"] = {"ok": True, "detail": "filter built (libseccomp)"}
    except OSError as err:
        layers.checks["seccomp"] = {"ok": False, "detail": f"libseccomp could not build the filter: {err}"}

    layers.checks["limits"] = (
        {
            "ok": True,
            "detail": "prlimit: data size, CPU, files; wall clock by the runner; no cgroups in a container",
        }
        if which("prlimit")
        else {"ok": False, "detail": "prlimit (util-linux) is not installed"}
    )
    return layers
