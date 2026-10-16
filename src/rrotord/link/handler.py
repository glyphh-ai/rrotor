"""What the Cloud's messages do here: a welcome's licence is kept, a call opens a run, continue and cancel go to it,
deploy, activate and remove change the versions. Every run the Cloud called answers its Results up the link; a
deploy answers `deployed`, or an event `deploy_failed` (protocol 1 has no refusal of its own)."""

from __future__ import annotations

import logging
from typing import Any

from rrotord.access import Access
from rrotord.deploy import Deployer
from rrotord.errors import RrotorError
from rrotord.licence import Licence
from rrotord.link import protocol as p
from rrotord.link.client import LinkClient
from rrotord.models import Models
from rrotord.runner.engine import Runner, failed
from rrotord.runner.record import Run

log = logging.getLogger("rrotor.link")


class LinkHandler:
    def __init__(
        self,
        models: Models,
        runner: Runner,
        deployer: Deployer,
        licence: Licence | None = None,
        access: Access | None = None,
    ) -> None:
        self.models, self.runner, self.deployer, self.licence, self.access = models, runner, deployer, licence, access
        self.link: LinkClient | None = None

    def deployed(self) -> list[dict[str, Any]]:
        return self.models.deployed()

    async def on_welcome(self, message: p.Welcome) -> None:
        """The licence and the keys the welcome carries (a hosted rrotor's only way to either) are kept."""
        if message.keys is not None and self.access is not None:
            self.access.set_keys(message.keys)
        if message.licence is None or self.licence is None:
            return
        try:
            self.licence.set(message.licence.payload, message.licence.signature)
        except RrotorError as err:
            log.warning("the welcome's licence was refused", extra={"code": err.code, "reason": err.message})

    async def on_access(self, message: p.Keys | p.Pause | p.Resume) -> None:
        """The keys changed, or the plan's hours ran out (or came back)."""
        if self.access is None:
            return
        if isinstance(message, p.Keys):
            self.access.set_keys(message.hashes)
        elif isinstance(message, p.Pause):
            self.access.pause(message.reason)
            log.warning("paused by the Cloud", extra={"reason": message.reason})
        else:
            self.access.resume()
            log.info("resumed by the Cloud")

    async def notify(self, run: Run) -> None:
        """A Result of a run the Cloud called goes up the link, numbered, until acknowledged."""
        if run.origin == "link" and self.link is not None and run.result is not None:
            outcome, seconds = run.result, run.seconds
            await self.link.owe(lambda seq: p.result(seq, run.id, outcome, seconds))

    async def _answer(self, run_id: str, outcome: dict[str, Any]) -> None:
        if self.link is not None:
            await self.link.owe(lambda seq: p.result(seq, run_id, outcome, 0))

    async def on_call(self, message: p.Call) -> None:
        try:
            await self.runner.call(
                model=message.model,
                function=message.function,
                inputs=message.inputs,
                version=message.version,
                caller=message.caller,
                origin="link",
                run_id=str(message.run_id),
                clearance=message.clearance,
            )
        except RrotorError as err:
            await self._answer(str(message.run_id), failed("call", err.code, err.message))

    async def on_continue(self, message: p.Continue) -> None:
        try:
            await self.runner.resume(str(message.run_id), message.answer)
        except RrotorError as err:
            if err.code == "E_RUN_MISSING":
                await self._answer(str(message.run_id), failed("continue", err.code, "This rrotor has no such run."))
            else:
                log.warning("continue refused", extra={"run_id": str(message.run_id), "code": err.code})

    async def on_cancel(self, message: p.Cancel) -> None:
        try:
            await self.runner.cancel(str(message.run_id))
        except RrotorError as err:
            if err.code == "E_RUN_MISSING":
                await self._answer(
                    str(message.run_id), {"status": "cancelled", "data": None, "step": "cancel", "prompt": None}
                )
            else:
                log.info("cancel of an ended run", extra={"run_id": str(message.run_id)})

    async def on_deploy(self, message: p.Deploy) -> None:
        try:
            if message.type == "remove":
                found = self.models.by_sha(message.model, message.commit_sha)
                if found is not None:
                    self.models.remove(found)
                return
            if message.type == "deploy" and message.repo:
                version = await self.deployer.from_repo(
                    message.repo, message.commit_sha, message.token, message.version
                )
                if version.model != message.model:
                    self.models.remove(version)
                    raise RrotorError(
                        f"The commit's glyphh.json names {version.model}, not {message.model}.", code="E_MANIFEST"
                    )
            else:
                version = await self.deployer.held(message.model, message.commit_sha, message.version)
        except RrotorError as err:
            log.warning("deploy failed", extra={"model": message.model, "code": err.code})
            if self.link is not None:
                data = {
                    "version": message.version,
                    "commit_sha": message.commit_sha,
                    "code": err.code,
                    "message": err.message,
                }
                await self.link.owe(lambda seq: p.event(seq, message.model, "deploy_failed", data))
            return
        if self.link is not None:
            await self.link.owe(
                lambda _seq: p.deployed(version.model, version.version, version.commit_sha), numbered=False
            )
