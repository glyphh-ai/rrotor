"""rrotord: start the daemon with the settings in its environment."""

from __future__ import annotations

import asyncio
import os
import sys

from rrotord import config, log
from rrotord.errors import RrotorError


def main() -> int:
    log.setup(os.environ.get("RROTOR_LOG_LEVEL", "INFO"))
    try:
        settings = config.load()
    except RrotorError as err:
        print(f"rrotord: {err.code}: {err.message}", file=sys.stderr)
        return 2
    from rrotord.daemon import main as serve

    asyncio.run(serve(settings))  # pragma: no cover
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
