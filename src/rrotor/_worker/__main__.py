"""python -m rrotor._worker: the worker's entry in the sandbox; its channel is fd 3."""

from __future__ import annotations

import os
import sys

from rrotor._worker.channel import Channel
from rrotor._worker.landlock import Landlock
from rrotor._worker.loop import Worker


def main() -> int:
    """The runner names the channel's descriptor in RROTOR_CHANNEL_FD."""
    return Worker(Channel.from_fd(int(os.environ.get("RROTOR_CHANNEL_FD", "3"))), Landlock()).run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
