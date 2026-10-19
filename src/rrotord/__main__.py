"""rrotord: start the daemon with the settings in its environment; or, as `rrotord cluster join | leave <node> |
promote <node>`, change this node's cluster (rrotord/cluster/command.py)."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence

from rrotord import config, log
from rrotord.errors import RrotorError


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    log.setup(os.environ.get("RROTOR_LOG_LEVEL", "INFO"))
    try:
        settings = config.load()
    except RrotorError as err:
        print(f"rrotord: {err.code}: {err.message}", file=sys.stderr)
        return 2
    if args[:1] == ["cluster"]:
        from rrotord.cluster import command

        confirm = command.terminal if sys.stdin.isatty() else None
        code, said = asyncio.run(command.run(settings, args[1:], confirm))
        print(said, file=sys.stdout if code == 0 else sys.stderr)
        return code
    if args:
        print(f"usage: rrotord, or {command_usage()}", file=sys.stderr)
        return 2
    if settings.mode == "doorman":  # pragma: no cover
        from rrotord.doorman import main as door

        asyncio.run(door(settings))
        return 0
    from rrotord.daemon import main as serve

    asyncio.run(serve(settings))  # pragma: no cover
    return 0  # pragma: no cover


def command_usage() -> str:
    from rrotord.cluster.command import USAGE

    return USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
