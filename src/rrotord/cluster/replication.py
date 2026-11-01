"""Replication with a fixed head (docs/specs/cluster.md, 2 and 3): the head is the single writer of what syncs, and
ships it to every follower as one ordered log; a follower applies it in order, and a new or lagging one catches up
from a snapshot first.

**On the head.** A version made active (any deploy or activation, by the API, its MCP or the link) is a `deploy`
entry {program, version, commit_sha, manifest}; a version removed, `remove`; the runtime keys' list changed, `keys
{hashes}`; the licence key held changed, `licence {key}`. No program's state is in the log: a program with data keeps
it in its data folder on the head, and every call of it runs there (`head_only`). Each follower has a shipper: it
sends `append {term, entries, commit_seq}` past what the follower acknowledged, and waits for new entries when it is
in step; a follower that is new, or behind the log the head still holds (`RROTOR_CLUSTER_LOG_KEEP`), is sent
`snapshot` instead. The log is trimmed to what every member has applied.

**Raft's rules** (raft.py) decide what is sent and what is taken: each append names the entry before it (`prev_seq`,
`prev_term`) and the follower takes it only if its log holds that entry; a diverged tail it has not applied is
dropped, one it applied (a deposed head's) makes it ask for a snapshot. Every request and answer carries the term,
and a lower one is refused. **Commit:** without HA (Pro, Team) an entry is committed once on the head's disk; with
HA (Enterprise) once a majority of voters hold it.

**On a follower.** An `append`'s entries are appended to its own log, and those committed applied one by one:
`deploy` by fetching the version from the head (`GET /cluster/v2/versions/<program>/<commit>.tar`: its `src/` and
`venv/`, the head's build; a node of another architecture refuses it, `E_CLUSTER_ARCH`) and making it active,
`remove`, `keys` (the access list), `licence` (offered to its own licence, which checks the signature itself),
`member` (the members, which Raft took when the entry arrived), `head` (nothing), and `rows` (nothing: an earlier
release's Ada store entry, still in a log written before the upgrade). `applied_seq` is kept in
`cluster/state.json`. An entry that does not apply (a full disk, an unknown kind) stops the applying there: the node
goes not ready, logs it, and tries again with the head's next append. `snapshot`: the follower fetches `GET
/cluster/v2/snapshot` (one tar: every version, the keys, the licence, the seq and term it stands at, the members),
installs it and starts its log after that seq; a snapshot of another term than the head's request is refused. A
follower is ready once it has applied all the head had committed when it last heard, and stays ready unless it has
been behind for more than a second. A node made head applies all its log holds before it leads (`own`).

**Never syncs:** runs, sandboxes, parts, `runtime.json`, the link's outbox, audit, any program's data folder, the
vault (rrotord/vault.py: secrets are node-local, kept by the head) and `schedules.json` (rrotord/scheduler.py: the
head fires the schedules)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import shutil
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rrotord import manifest as manifests
from rrotord.access import Access
from rrotord.cluster.log import Log
from rrotord.cluster.raft import Raft, Timing
from rrotord.cluster.transport import Channel, ClusterError
from rrotord.errors import RrotorError
from rrotord.licence import Licence
from rrotord.programs import Programs, Version

log = logging.getLogger("rrotor.cluster")
ARCH = platform.machine()
IDLE = 5.0  # a shipper in step looks again at least this often (without HA: with it, every heartbeat)
LAG = 1.0  # a follower behind the head for longer than this is not ready
APPEND_WITHIN = 120.0  # an append's answer (a deploy entry fetches its version)
SNAPSHOT_WITHIN = 1800.0
BATCH = 200
TIMING = Timing()  # heartbeats every 250 ms, elections after 1.5 to 3 s (tests make them short)

Fetch = Callable[[str, Path], Awaitable[dict[str, str]]]  # a path on the head's listener, into a file; its headers


def described_version(v: Version) -> dict[str, Any]:
    return {"version": v.version, "commit_sha": v.commit_sha, "manifest": v.manifest.model_dump(mode="json")}


class ArchError(RrotorError):
    code, status = "E_CLUSTER_ARCH", 503


class Replication:
    def __init__(
        self,
        folder: Path,
        scratch: Path,
        keep: int,
        programs: Programs,
        access: Access,
        licence: Licence,
        role: Callable[[], str],
        saved: Callable[[int], None],
        applied: int = 0,
        wall: Callable[[], float] = time.time,
        *,
        me: str = "",
        state: dict[str, Any] | None = None,
        persist: Callable[[dict[str, Any]], None] = lambda _: None,
        ha: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.log = Log(folder, keep)
        self.scratch, self.programs, self.access, self.licence = scratch, programs, access, licence
        self.role, self.saved, self.wall, self.clock, self.ha = role, saved, wall, clock, ha
        self.raft = Raft(me, self.log, state or {}, ha=ha, save=persist, now=clock(), applied=applied, timing=TIMING)
        self.moved: Callable[[], None] = lambda: None  # Raft's role, term or head may have changed: the node looks
        self.lock = asyncio.Lock()  # the head's snapshots; a follower's applying
        self.acked: dict[str, int | None] = {}  # the head's: each follower's applied seq (None: a snapshot first)
        self.shippers: dict[str, asyncio.Task[None]] = {}
        self.woken: dict[str, asyncio.Event] = {}
        self.synced = False  # a follower's: it has applied what the head had when it last heard
        self.behind_since: float | None = None
        self.failure: dict[str, Any] | None = None  # a follower's: the entry that did not apply
        self.shipped_licence: Any = None
        self.heard = 0  # a follower's: the head's commit_seq as it last said
        programs.on_change = self.version_changed
        self.fetch: Fetch | None = None  # the follower's way to the head's listener (node.py)
        self.member: Callable[[dict[str, Any]], None] = lambda _: None  # a `member` entry applied (node.py)
        self.arch = ARCH  # this node's machine: one cluster is one architecture

    # What the rest reads

    @property
    def applied(self) -> int:
        """The last seq applied to this node (a head's: every one it holds)."""
        return self.raft.applied

    @applied.setter
    def applied(self, seq: int) -> None:
        self.raft.applied = seq

    @property
    def commit_seq(self) -> int:
        return self.raft.commit if self.role() == "head" else max(self.raft.commit, self.heard)

    @property
    def applied_seq(self) -> int:
        return self.log.last if self.role() == "head" else self.applied

    def current(self) -> bool:
        """A follower's readiness: in step with its head, or not behind it for more than LAG."""
        if not self.synced or self.failure is not None:
            return False
        return self.behind_since is None or self.wall() - self.behind_since <= LAG

    # The head: what syncs

    def _append(self, kind: str, **body: Any) -> dict[str, Any]:
        entry = self.raft.propose({"kind": kind, **body})
        self.wake()
        return entry

    def wake(self) -> None:
        """Every shipper: there is something to send."""
        for event in self.woken.values():
            event.set()

    def version_changed(self, kind: str, found: Version) -> None:
        """A version made active or removed on the head (programs.py): a `deploy` or `remove` entry."""
        if self.role() != "head":
            return
        body = {"program": found.program, "version": found.version, "commit_sha": found.commit_sha}
        if kind == "remove":
            self._append("remove", **body)
        else:
            self._append("deploy", **body, manifest=found.manifest.model_dump(mode="json"))

    def keys_changed(self) -> None:
        if self.role() == "head":
            self._append("keys", hashes=list(self.access.keys))

    def licence_changed(self) -> None:
        held = self.licence.held
        if self.role() == "head" and held is not None and held.raw != self.shipped_licence:
            self.shipped_licence = held.raw
            self._append("licence", key=held.raw)

    def lead(self) -> None:
        """This node became the head: what it holds is what syncs from here."""
        held = self.licence.held
        self.shipped_licence = held.raw if held is not None else None

    async def stop(self) -> None:
        tasks = list(self.shippers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.shippers.clear()
        self.woken.clear()

    # The head: shipping

    def attach(self, node: str, channel: Channel, applied: Any) -> None:
        """A follower joined (or came back): its shipper starts from what it says it applied (a new node, or one
        that never applied anything, with a snapshot)."""
        self.detach(node)
        self.raft.attach(node, applied)
        if isinstance(applied, int) and not isinstance(applied, bool):
            self.acked[node] = applied
        self.woken[node] = asyncio.Event()
        self.shippers[node] = asyncio.create_task(self._ship(node, channel))

    def detach(self, node: str) -> None:
        task = self.shippers.pop(node, None)
        if task is not None:
            task.cancel()
        self.woken.pop(node, None)

    def heard_from(self, node: str, applied: Any) -> None:
        if isinstance(applied, int) and not isinstance(applied, bool):
            self.acked[node] = applied

    def trim(self, members: list[str]) -> None:
        """The log kept until every member has applied it."""
        others = [self.acked.get(m) or 0 for m in members]
        if others:
            self.log.trim(min(others))

    async def _ship(self, node: str, channel: Channel) -> None:
        """One follower's shipper: Raft's next request of it (entries past what it holds, a heartbeat when in step,
        or a snapshot), its answer given back to Raft; at once again while there is more to send, else when woken or
        a heartbeat (HA) or IDLE has passed."""
        woken = self.woken[node]
        try:
            while self.raft.role == "head":
                asked = self.raft.message_for(node)
                woken.clear()
                kind, body = asked["type"], {k: v for k, v in asked.items() if k != "type"}
                within = SNAPSHOT_WITHIN if kind == "snapshot" else APPEND_WITHIN
                alive = asyncio.create_task(self._alive(node))  # a long one (a snapshot, a version): still in touch
                try:
                    got: dict[str, Any] | None = await channel.request(kind, within=within, **body)
                except ClusterError as err:
                    if err.code == "E_CLUSTER_CLOSED":
                        raise
                    log.warning("cluster: a follower did not take a " + kind, extra={"node": node, "code": err.code})
                    got = None
                finally:
                    alive.cancel()
                if got is not None:
                    self.raft.answered(node, asked, got, self.clock())
                    self.heard_from(node, got.get("applied_seq"))
                    self.moved()
                    at = self.raft.next.get(node)
                    if at is None or at <= self.log.last:  # a snapshot to send, or more entries
                        continue
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(woken.wait(), self.raft.timing.heartbeat if self.ha() else IDLE)
        except ClusterError as err:
            log.warning("cluster: shipping to a follower stopped", extra={"node": node, "code": err.code})
        except TimeoutError:
            log.warning("cluster: a follower did not answer; shipping to it stopped", extra={"node": node})

    async def _alive(self, node: str) -> None:
        """While a request is with a follower, on a connection still open, the follower counts as heard from."""
        while True:
            await asyncio.sleep(self.raft.timing.heartbeat)
            self.raft.heard[node] = self.clock()

    # The head: what a follower fetches

    def version_tar(self, program: str, commit: str) -> Path:
        """A version's `src/` and `venv/` as a tar in the scratch folder (the caller removes it)."""
        found = self.programs.by_sha(program, commit)
        if found is None:
            raise RrotorError(
                f"This node does not hold {program} at {commit[:12]}.", code="E_VERSION_MISSING", status=404
            )
        out = self._scratch(".tar")
        with tarfile.open(out, "w") as tar:
            tar.add(found.src, "src")
            tar.add(found.venv, "venv")
        return out

    def _scratch(self, suffix: str) -> Path:
        self.scratch.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.scratch, suffix=suffix)
        os.close(fd)
        return Path(name)

    async def snapshot(self) -> tuple[Path, int]:
        """Everything that syncs as one tar, and the seq it stands at."""
        async with self.lock:
            return await asyncio.to_thread(self._snapshot)

    def _snapshot(self) -> tuple[Path, int]:
        seq = self.log.last
        work = Path(tempfile.mkdtemp(dir=self._folder()))
        try:
            described: dict[str, Any] = {}
            out = self._scratch(".tar")
            with tarfile.open(out, "w") as tar:
                for name in self.programs.names():
                    active = self.programs.active(name)
                    versions = self.programs.versions(name)
                    described[name] = {
                        "active": active.commit_sha if active else None,
                        "versions": [described_version(v) for v in versions],
                    }
                    for v in versions:
                        tar.add(v.src, f"versions/{name}/{v.commit_sha}/src")
                        tar.add(v.venv, f"versions/{name}/{v.commit_sha}/venv")
                held = self.licence.held
                about = {
                    "seq": seq,
                    "seq_term": self.log.term_at(seq),
                    "term": self.raft.term,
                    "members": self.raft.members,
                    "members_at": list(self.raft.members_at),
                    "arch": self.arch,
                    "keys": list(self.access.keys),
                    "licence": held.raw if held is not None else None,
                    "programs": described,
                }
                meta = work / "snapshot.json"
                meta.write_text(json.dumps(about))
                tar.add(meta, "snapshot.json")
            return out, seq
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _folder(self) -> Path:
        self.scratch.mkdir(parents=True, exist_ok=True)
        return self.scratch

    # A follower

    async def receive(self, msg: dict[str, Any]) -> dict[str, Any]:
        """An `append` from the head: Raft's answer (what this log now holds, or why not), once what is committed
        is applied in order, with the seq applied."""
        async with self.lock:
            reply = self.raft.on_append(msg, self.clock())
            self.moved()
            if reply.get("accepted"):
                heard = msg.get("commit_seq")
                self.heard = heard if isinstance(heard, int) else self.heard
                await self._apply_committed()
            return {**reply, "applied_seq": self.applied}

    async def _apply_committed(self) -> None:
        """What is committed and not applied yet, applied in order; an entry that does not apply stops it there."""
        if await self._apply_upto(min(self.raft.commit, self.log.last)):
            self.failure = None
            self._caught_up()

    async def _apply_upto(self, upto: int) -> bool:
        while self.applied < upto:
            for entry in self.log.read(self.applied, BATCH):
                if entry["seq"] > upto:
                    break
                try:
                    await self._apply(entry)
                except (RrotorError, OSError, ValueError, KeyError, tarfile.TarError) as err:
                    self._failed(entry, err)
                    return False
                self.applied = entry["seq"]
                self.saved(self.applied)
        return True

    async def own(self) -> bool:
        """A node made head: all its log holds applied before it writes (Raft's head commits it all), and the
        seq kept. False when an entry does not apply: it cannot lead on a node behind its log."""
        async with self.lock:
            done = await self._apply_upto(self.log.last)
        if not done:
            log.error("cluster: made head, but its log does not apply: it does not lead", extra=self.failure)
        return done

    def _caught_up(self) -> None:
        if self.applied >= self.heard:
            self.synced, self.behind_since = True, None
        elif self.behind_since is None:
            self.behind_since = self.wall()

    def _failed(self, entry: dict[str, Any], err: BaseException) -> None:
        code = err.code if isinstance(err, RrotorError) else "E_CLUSTER_APPLY"
        self.failure = {"seq": entry.get("seq"), "kind": entry.get("kind"), "code": code, "reason": str(err)}
        log.error("cluster: an entry did not apply; this node stops applying and is not ready", extra=self.failure)

    async def _apply(self, entry: dict[str, Any]) -> None:
        kind = entry.get("kind")
        if "model" in entry and kind in ("deploy", "remove"):  # in this node's log since an earlier release
            entry = {**entry, "program": entry["model"]}
        if kind == "deploy":
            await self._deploy(entry)
        elif kind == "remove":
            found = self.programs.by_sha(str(entry["program"]), str(entry["commit_sha"]))
            if found is not None:
                self.programs.remove(found)
        elif kind == "keys":
            self.access.set_keys(list(entry.get("hashes") or []))
        elif kind == "licence":
            if entry.get("key") is not None:
                self.licence.offer(entry["key"], "cluster")
        elif kind == "member":
            self.member(entry)
        elif kind not in ("head", "rows"):  # a head's first entry in its term, an earlier release's rows: nothing
            raise RrotorError(f"An entry of a kind this node does not know: {kind!r}.", code="E_CLUSTER_KIND")

    async def _deploy(self, entry: dict[str, Any]) -> None:
        program, commit = str(entry["program"]), str(entry["commit_sha"])
        manifest = manifests.Manifest.model_validate(entry["manifest"])
        if self.programs.by_sha(program, commit) is None:
            tar = self._scratch(".tar")
            try:
                headers = await self._fetch(f"/cluster/v2/versions/{program}/{commit}.tar", tar)
                arch = headers.get("Rrotor-Arch")
                if arch != self.arch:
                    raise ArchError(f"The head is {arch} and this node {self.arch}: one cluster is one architecture.")
                self._unpack(tar, {"src": "src", "venv": "venv"}, self.programs.version_root(program, commit))
            finally:
                tar.unlink(missing_ok=True)
        found = self.programs.record(program, int(entry["version"]), commit, manifest)
        self.programs.activate(found)

    async def _fetch(self, path: str, dest: Path) -> dict[str, str]:
        if self.fetch is None:
            raise ClusterError("No head to fetch from.", code="E_NO_HEAD")
        return await self.fetch(path, dest)

    def _unpack(self, tar: Path, parts: dict[str, str], root: Path) -> None:
        """Members of a tar under each prefix, into root/<name> (staged, then moved into place)."""
        staging = root.with_name(f".{root.name}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with tarfile.open(tar) as t:
                for prefix, name in parts.items():
                    members = []
                    for m in t.getmembers():
                        if m.name == prefix or m.name.startswith(prefix + "/"):
                            m.name = name + m.name[len(prefix) :]
                            members.append(m)
                    t.extractall(staging, members=members, filter="tar")
            shutil.rmtree(root, ignore_errors=True)
            staging.rename(root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def install(self, msg: dict[str, Any]) -> dict[str, Any]:
        """`snapshot` from the head: fetched, installed, and the log begun after its seq. Answers as Raft does, with
        the seq applied; refused (this node's term) from a head of a lower term."""
        async with self.lock:
            refused = self.raft.on_snapshot(msg, self.clock())
            self.moved()
            if refused is not None:
                return {**refused, "applied_seq": self.applied}
            tar = self._scratch(".tar")
            try:
                await self._fetch("/cluster/v2/snapshot", tar)
                about = await asyncio.to_thread(self._install, tar, msg.get("term"))
            except (RrotorError, OSError, ValueError, KeyError, tarfile.TarError) as err:
                self._failed({"seq": None, "kind": "snapshot"}, err)
                assert self.failure is not None  # noqa: S101  (just set)
                raise RrotorError(str(err), code=str(self.failure["code"]), status=503) from None
            finally:
                tar.unlink(missing_ok=True)
            seq = int(about["seq"])
            reply = self.raft.installed(
                seq,
                int(about.get("seq_term") or 0),
                list(about.get("members") or []),
                about.get("members_at"),
                int(msg.get("commit_seq") or 0),
            )
            self.heard = max(self.heard, seq)
            self.saved(seq)
            self.failure, self.synced, self.behind_since = None, True, None
            log.info("cluster: snapshot installed", extra={"seq": seq})
            return {**reply, "applied_seq": seq}

    def _install(self, tar: Path, term: Any) -> dict[str, Any]:
        with tarfile.open(tar) as t:
            member = t.extractfile("snapshot.json")
            about: dict[str, Any] = json.loads(member.read() if member is not None else b"")  # a folder: not JSON
        if about.get("term") != term:
            raise RrotorError("The snapshot is of another term than the head that asked for it.", code="E_CLUSTER_TERM")
        if about.get("arch") != self.arch:
            raise ArchError(
                f"The head is {about.get('arch')} and this node {self.arch}: one cluster is one architecture."
            )
        programs: dict[str, Any] = about["programs"]
        for name, described in programs.items():
            keep = set()
            for v in described["versions"]:
                sha = str(v["commit_sha"])
                keep.add(sha)
                if self.programs.by_sha(name, sha) is None:
                    prefix = f"versions/{name}/{sha}"
                    self._unpack(
                        tar, {f"{prefix}/src": "src", f"{prefix}/venv": "venv"}, self.programs.version_root(name, sha)
                    )
                self.programs.record(name, int(v["version"]), sha, manifests.Manifest.model_validate(v["manifest"]))
            for held in self.programs.versions(name):
                if held.commit_sha not in keep:
                    self.programs.remove(held)
            active = self.programs.by_sha(name, described["active"]) if described.get("active") else None
            if active is not None:
                self.programs.activate(active)
        self.access.set_keys(list(about.get("keys") or []))
        if about.get("licence") is not None:
            self.licence.offer(about["licence"], "cluster")
        return about
