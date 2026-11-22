"""What the Cloud's messages do here: a welcome's licence key is offered (and its word on the apps kept), a call
opens a run, continue and cancel go to it, deploy, activate and remove change the versions, `app` keeps an app's
review (an approved version activated). Every run the Cloud called answers its Results up the link; a deploy answers
`deployed`, or an event `deploy_failed` (the protocol has no refusal of its own). Every other deploy (the API's and
its MCP's) reports `deployed` through here too.

What the Cloud asks for is done as `server` (rrotord/audit.py): its calls, continues and cancels are audit events
like any caller's, and its deploys, activations and removals are `program.deploy`, `program.activate`,
`program.remove` or `program.deploy_failed`."""

from __future__ import annotations

import logging
from typing import Any

from rrotord.access import Access
from rrotord.audit import SERVER, Audit, acting
from rrotord.catalogue import Catalogue
from rrotord.deploy import Deployer
from rrotord.errors import RrotorError
from rrotord.licence import Licence
from rrotord.link import protocol as p
from rrotord.link.client import LinkClient
from rrotord.programs import Programs, Version
from rrotord.runner.engine import Runner, failed
from rrotord.runner.record import Run

log = logging.getLogger("rrotor.link")


class LinkHandler:
    def __init__(
        self,
        programs: Programs,
        runner: Runner,
        deployer: Deployer,
        licence: Licence | None = None,
        access: Access | None = None,
        audit: Audit | None = None,
        catalogue: Catalogue | None = None,
    ) -> None:
        self.programs, self.runner, self.deployer, self.licence, self.access = (
            programs,
            runner,
            deployer,
            licence,
            access,
        )
        self.audit = audit
        self.catalogue = catalogue
        self.link: LinkClient | None = None

    def _audit(self, kind: str, **detail: Any) -> None:
        if self.audit is not None:
            self.audit.emit(kind, who=SERVER, **detail)

    def deployed(self) -> list[dict[str, Any]]:
        return self.programs.deployed()

    async def on_welcome(self, message: p.Welcome) -> None:
        """The runtime keys the welcome carries are kept, and its licence key (and, release N, its legacy licence)
        offered: a refused one is logged by the licence, never fatal."""
        if message.keys is not None and self.access is not None:
            self.access.set_keys(message.keys)
        if message.apps is not None and self.catalogue is not None:
            self.catalogue.set_all([a.model_dump() for a in message.apps], message.reviewers)
            for reviewed in message.apps:
                if reviewed.state == "approved":
                    self._activate(reviewed.program, reviewed.version)
        if self.licence is None:
            return
        if message.license_key is not None:
            self.licence.offer(message.license_key, "welcome")
        if message.licence is not None:
            self.licence.offer(message.licence.model_dump(), "welcome")

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

    async def on_app(self, message: p.App) -> None:
        """The catalogue's word on an app changed: kept; an approved version is the one served, and activated when
        this rrotor holds it and another was active (docs/architecture.md, 5.8)."""
        if self.catalogue is None:
            return
        self.catalogue.set_one(message.program, message.version, message.state, message.roles)
        log.info("app %s", message.state, extra={"program": message.program, "version": message.version})
        if message.state == "approved":
            self._activate(message.program, message.version)

    def _activate(self, program: str, version: int) -> None:
        try:
            found = self.programs.get(program, version)
        except RrotorError:
            return  # not held here (yet): nothing to activate
        active = self.programs.active(program)
        if active is not None and active.commit_sha == found.commit_sha:
            return
        self.programs.activate(found)
        self._audit(
            "program.activate",
            program=found.program,
            version=found.version,
            commit_sha=found.commit_sha,
            outcome={"status": "done", "code": None},
        )

    async def report(self, version: Version, repo: str | None, by: str | None = None) -> None:
        """`deployed` up the link after every deploy, however it was asked for (not numbered, kept until
        acknowledged), with `by` for an app a person published; nothing while this rrotor has no runtime token."""
        if self.link is None or not self.link.configured:
            return
        manifest = version.manifest.model_dump(mode="json")
        await self.link.owe(
            lambda _seq: p.deployed(version.program, version.version, version.commit_sha, repo, manifest, by),
            numbered=False,
        )

    async def notify(self, run: Run) -> None:
        """A Result of a run the Cloud called goes up the link, numbered, until acknowledged."""
        if run.origin == "link" and self.link is not None and run.result is not None:
            outcome, seconds = run.result, run.seconds
            await self.link.owe(lambda seq: p.result(seq, run.id, outcome, seconds))

    async def _answer(self, run_id: str, outcome: dict[str, Any]) -> None:
        if self.link is not None:
            await self.link.owe(lambda seq: p.result(seq, run_id, outcome, 0))

    async def on_call(self, message: p.Call) -> None:
        with acting(SERVER):
            await self._call(message)

    async def _call(self, message: p.Call) -> None:
        try:
            await self.runner.call(
                program=message.program,
                function=message.function,
                inputs=message.inputs,
                version=message.version,
                caller=message.caller,
                origin="link",
                run_id=str(message.run_id),
                clearance=message.clearance,
                folders=message.folders,
            )
        except RrotorError as err:
            await self._answer(str(message.run_id), failed("call", err.code, err.message))

    async def on_continue(self, message: p.Continue) -> None:
        with acting(SERVER):
            await self._continue(message)

    async def _continue(self, message: p.Continue) -> None:
        try:
            await self.runner.resume(str(message.run_id), message.answer)
        except RrotorError as err:
            if err.code == "E_RUN_MISSING":
                await self._answer(str(message.run_id), failed("continue", err.code, "This rrotor has no such run."))
            else:
                log.warning("continue refused", extra={"run_id": str(message.run_id), "code": err.code})

    async def on_cancel(self, message: p.Cancel) -> None:
        with acting(SERVER):
            await self._cancel(message)

    async def _cancel(self, message: p.Cancel) -> None:
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
                found = self.programs.by_sha(message.program, message.commit_sha)
                if found is not None:
                    self.programs.remove(found)
                    self._audit(
                        "program.remove",
                        program=found.program,
                        version=found.version,
                        commit_sha=found.commit_sha,
                        outcome={"status": "done", "code": None},
                    )
                return
            repo = message.repo if message.type == "deploy" and message.repo else None
            if repo is not None:
                version = await self.deployer.from_repo(repo, message.commit_sha, message.token, message.version)
                if version.program != message.program:
                    self.programs.remove(version)
                    raise RrotorError(
                        f"The commit's glyphh.json names {version.program}, not {message.program}.", code="E_MANIFEST"
                    )
            else:
                version = await self.deployer.held(message.program, message.commit_sha, message.version)
        except RrotorError as err:
            log.warning("deploy failed", extra={"program": message.program, "code": err.code})
            self._audit(
                "program.deploy_failed",
                program=message.program,
                version=message.version,
                commit_sha=message.commit_sha,
                outcome={"status": "failed", "code": err.code},
            )
            if self.link is not None:
                data = {
                    "version": message.version,
                    "commit_sha": message.commit_sha,
                    "code": err.code,
                    "message": err.message,
                }
                await self.link.owe(lambda seq: p.event(seq, message.program, "deploy_failed", data))
            return
        self._audit(
            "program.activate" if message.type == "activate" else "program.deploy",
            program=version.program,
            version=version.version,
            commit_sha=version.commit_sha,
            repo=repo,
            outcome={"status": "done", "code": None},
        )
        await self.report(version, repo)
