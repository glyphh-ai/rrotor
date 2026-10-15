"""Short-lived commands (git, uv, venv), with a time limit; their output is kept for an error, masked."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from rrotord.log import mask


@dataclass(frozen=True, slots=True)
class Done:
    code: int
    stdout: str
    stderr: str

    def why(self, limit: int = 800) -> str:
        text = (self.stderr.strip() or self.stdout.strip() or f"exit {self.code}")[-limit:]
        return mask(text)


async def run(
    argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None, timeout: float = 600.0
) -> Done:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return Done(-9, "", f"{argv[0]} took longer than {timeout:g} s")
    return Done(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
