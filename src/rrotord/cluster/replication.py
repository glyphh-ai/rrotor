"""Replication with a fixed head (docs/specs/cluster.md, 2 and 3): the head is the single writer of what syncs, and
ships it to every follower as one ordered log; a follower applies it in order, and a new or lagging one catches up
from a snapshot first.

**On the head.** Every run of an Ada model is bracketed (`before`, `after`, and every second while runs are in
hand): the store's change capture is installed (capture.py) and what changed since is appended to the log as one
`rows` entry per model; a store captured for the first time is shipped whole first (`rows` with `reset` and its
schema). A version made active (any deploy or activation, by the API, its MCP or the link) is a `deploy` entry
{model, version, commit_sha, manifest}; a version removed, `remove`; the runtime keys' list changed, `keys
{hashes}`; the licence key held changed, `licence {key}`. Each follower has a shipper: it sends `append {term,
entries, commit_seq}` past what the follower acknowledged, and waits for new entries when it is in step; a follower
that is new, or behind the log the head still holds (`RROTOR_CLUSTER_LOG_KEEP`), is sent `snapshot` instead. The log
is trimmed to what every member has applied.

**Commit (Pro and Team):** an entry is committed once it is on the head's disk, so `commit_seq` is the head's last
seq. A majority commit (Enterprise) comes with elections.

**On a follower.** An `append`'s entries are appended to its own log and applied one by one: `rows` in one SQLite
transaction on the replica (capture.py), `deploy` by fetching the version from the head (`GET
/cluster/v1/versions/<model>/<commit>.tar`: its `src/` and `venv/`, the head's build; a node of another architecture
refuses it, `E_CLUSTER_ARCH`) and making it active, `remove`, `keys` (the access list), `licence` (offered to its own
licence, which checks the signature itself). `applied_seq` is kept in `cluster/state.json`. An entry that does not
apply (a full disk, an unknown kind) stops the applying there: the node goes not ready, logs it, and tries again with
the head's next append. `snapshot`: the follower fetches `GET /cluster/v1/snapshot` (one tar: every Ada store by
SQLite's online backup, every version, the keys, the licence, the seq it stands at), installs it and starts its log
after that seq. A follower is ready once it has applied all the head had committed when it last heard, and stays
ready unless it has been behind for more than a second.

**Never syncs:** runs, sandboxes, parts, `runtime.json`, the link's outbox, audit, and any data-folder file but
`precedents.sqlite`. A model with data that is not Ada (`head_only`) runs on the head only."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import shutil
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rrotor import ada
from rrotord import manifest as manifests
from rrotord.access import Access
from rrotord.cluster import capture
from rrotord.cluster.log import Log
from rrotord.cluster.transport import Channel, ClusterError
from rrotord.errors import RrotorError
from rrotord.licence import Licence
from rrotord.models import Models, Version

log = logging.getLogger("rrotor.cluster")
ARCH = platform.machine()
CAPTURE_EVERY = 1.0  # while runs are in hand, the head ships what they changed this often
IDLE = 5.0  # a shipper in step looks again at least this often
LAG = 1.0  # a follower behind the head for longer than this is not ready
APPEND_WITHIN = 120.0  # an append's answer (a deploy entry fetches its version)
SNAPSHOT_WITHIN = 1800.0
BATCH = 200

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
        models: Models,
        access: Access,
        licence: Licence,
        role: Callable[[], str],
        term: Callable[[], int],
        saved: Callable[[int], None],
        applied: int = 0,
        busy: Callable[[], bool] = lambda: False,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.log = Log(folder, keep)
        self.scratch, self.models, self.access, self.licence = scratch, models, access, licence
        self.role, self.term, self.saved, self.busy, self.wall = role, term, saved, busy, wall
        self.applied = applied  # a follower's: the last seq applied
        self.lock = asyncio.Lock()  # the head's capture and snapshots; a follower's applying
        self.acked: dict[str, int | None] = {}  # the head's: each follower's applied seq (None: a snapshot first)
        self.shippers: dict[str, asyncio.Task[None]] = {}
        self.woken: dict[str, asyncio.Event] = {}
        self.ticker: asyncio.Task[None] | None = None
        self.synced = False  # a follower's: it has applied what the head had when it last heard
        self.behind_since: float | None = None
        self.failure: dict[str, Any] | None = None  # a follower's: the entry that did not apply
        self.shipped_licence: Any = None
        self.heard = 0  # a follower's: the head's commit_seq as it last said
        models.on_change = self.version_changed
        self.fetch: Fetch | None = None  # the follower's way to the head's listener (node.py)
        self.arch = ARCH  # this node's machine: one cluster is one architecture

    # What the rest reads

    @property
    def commit_seq(self) -> int:
        return self.log.last if self.role() == "head" else max(self.applied, self.heard)

    @property
    def applied_seq(self) -> int:
        return self.log.last if self.role() == "head" else self.applied

    def current(self) -> bool:
        """A follower's readiness: in step with its head, or not behind it for more than LAG."""
        if not self.synced or self.failure is not None:
            return False
        return self.behind_since is None or self.wall() - self.behind_since <= LAG

    # The head: capture

    def _append(self, kind: str, **body: Any) -> dict[str, Any]:
        entry = self.log.append({"term": self.term(), "kind": kind, **body})
        for event in self.woken.values():
            event.set()
        return entry

    def _store(self, model: str) -> Path:
        return self.models.data(model) / ada.STORE

    def _capture(self, model: str) -> None:
        """What changed in a model's store, appended as one `rows` entry (the whole store, the first time)."""
        store = self._store(model)
        if not store.is_file():
            return
        if capture.install(store):
            whole = capture.everything(store)
            self._append("rows", model=model, reset=True, schema=whole["schema"], rows=whole["rows"])
        rows, upto = capture.collect(store)
        if rows:
            self._append("rows", model=model, rows=rows)
        if upto:
            capture.clear(store, upto)

    def _ada(self) -> list[str]:
        return [n for n in self.models.names() if any("ada" in v.manifest.needs for v in self.models.versions(n))]

    async def capture(self, model: str) -> None:
        if self.role() != "head":
            return
        async with self.lock:
            try:
                await asyncio.to_thread(self._capture, model)
            except sqlite3.Error as err:
                log.error("cluster: a store's changes were not captured", extra={"model": model, "reason": str(err)})

    async def before(self, found: Version) -> None:
        """Before a run on the head: the store's capture in place (and the store shipped whole, the first time)."""
        if "ada" in found.manifest.needs:
            await self.capture(found.model)

    async def after(self, model: str) -> None:
        """After a run on the head (or its step): what it changed, shipped."""
        await self.capture(model)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(CAPTURE_EVERY)
            if self.busy():
                for model in self._ada():
                    await self.capture(model)

    def version_changed(self, kind: str, found: Version) -> None:
        """A version made active or removed on the head (models.py): a `deploy` or `remove` entry."""
        if self.role() != "head":
            return
        body = {"model": found.model, "version": found.version, "commit_sha": found.commit_sha}
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
        if self.ticker is None:
            self.ticker = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        tasks = [*self.shippers.values(), *([self.ticker] if self.ticker else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.shippers.clear()
        self.woken.clear()
        self.ticker = None

    # The head: shipping

    def attach(self, node: str, channel: Channel, applied: Any) -> None:
        """A follower joined (or came back): its shipper starts from what it says it applied."""
        self.detach(node)
        known = isinstance(applied, int) and not isinstance(applied, bool) and applied > 0
        self.acked[node] = applied if known else None  # a new node (or one that never applied anything): a snapshot
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
        woken = self.woken[node]
        try:
            while True:
                at = self.acked.get(node)
                if at is None or at < self.log.first - 1 or at > self.log.last:
                    try:
                        got = await channel.request("snapshot", term=self.term(), within=SNAPSHOT_WITHIN)
                    except ClusterError as err:
                        if err.code == "E_CLUSTER_CLOSED":
                            raise
                        log.warning(
                            "cluster: a follower did not take a snapshot", extra={"node": node, "code": err.code}
                        )
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(woken.wait(), IDLE)
                        continue
                    self.acked[node] = int(got["applied_seq"])
                    continue
                woken.clear()
                entries = self.log.read(at, BATCH)
                got = await channel.request(
                    "append", term=self.term(), entries=entries, commit_seq=self.log.last, within=APPEND_WITHIN
                )
                applied = int(got["applied_seq"])
                self.acked[node] = applied
                if not entries or applied <= at:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(woken.wait(), IDLE)
        except ClusterError as err:
            log.warning("cluster: shipping to a follower stopped", extra={"node": node, "code": err.code})
        except TimeoutError:
            log.warning("cluster: a follower did not answer; shipping to it stopped", extra={"node": node})

    # The head: what a follower fetches

    def version_tar(self, model: str, commit: str) -> Path:
        """A version's `src/` and `venv/` as a tar in the scratch folder (the caller removes it)."""
        found = self.models.by_sha(model, commit)
        if found is None:
            raise RrotorError(
                f"This node does not hold {model} at {commit[:12]}.", code="E_VERSION_MISSING", status=404
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
        """Everything that syncs as one tar, and the seq it stands at: every Ada store's changes shipped first, then
        each copied by SQLite's online backup. A change a run makes meanwhile is in the copy and in a later entry,
        which applies again to the same row: the replica ends the same."""
        async with self.lock:
            return await asyncio.to_thread(self._snapshot)

    def _snapshot(self) -> tuple[Path, int]:
        for model in self._ada():
            self._capture(model)
        seq = self.log.last
        work = Path(tempfile.mkdtemp(dir=self._folder()))
        try:
            described: dict[str, Any] = {}
            out = self._scratch(".tar")
            with tarfile.open(out, "w") as tar:
                for name in self.models.names():
                    active = self.models.active(name)
                    versions = self.models.versions(name)
                    described[name] = {
                        "active": active.commit_sha if active else None,
                        "versions": [described_version(v) for v in versions],
                    }
                    for v in versions:
                        tar.add(v.src, f"versions/{name}/{v.commit_sha}/src")
                        tar.add(v.venv, f"versions/{name}/{v.commit_sha}/venv")
                    store = self._store(name)
                    if store.is_file():
                        copy = work / f"{name}.sqlite"
                        capture.backup(store, copy)
                        tar.add(copy, f"stores/{name}.sqlite")
                held = self.licence.held
                about = {
                    "seq": seq,
                    "term": self.term(),
                    "arch": self.arch,
                    "keys": list(self.access.keys),
                    "licence": held.raw if held is not None else None,
                    "models": described,
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

    async def receive(self, msg: dict[str, Any]) -> int:
        """An `append` from the head: its entries kept and applied in order. Answers the seq applied."""
        async with self.lock:
            heard = msg.get("commit_seq")
            self.heard = heard if isinstance(heard, int) else self.heard
            for entry in msg.get("entries") or []:
                if not isinstance(entry, dict) or not isinstance(entry.get("seq"), int):
                    continue
                if entry["seq"] <= self.applied:
                    continue  # had already
                try:
                    if entry["seq"] > self.log.last:
                        self.log.append(dict(entry))
                    await self._apply(entry)
                except (RrotorError, OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError) as err:
                    self._failed(entry, err)
                    return self.applied
                self.applied = entry["seq"]
                self.saved(self.applied)
            self.failure = None
            self._caught_up()
            return self.applied

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
        if kind == "rows":
            store = self._store(str(entry["model"]))
            await asyncio.to_thread(
                capture.apply, store, list(entry.get("rows") or []), entry.get("schema"), bool(entry.get("reset"))
            )
        elif kind == "deploy":
            await self._deploy(entry)
        elif kind == "remove":
            found = self.models.by_sha(str(entry["model"]), str(entry["commit_sha"]))
            if found is not None:
                self.models.remove(found)
        elif kind == "keys":
            self.access.set_keys(list(entry.get("hashes") or []))
        elif kind == "licence":
            if entry.get("key") is not None:
                self.licence.offer(entry["key"], "cluster")
        else:
            raise RrotorError(f"An entry of a kind this node does not know: {kind!r}.", code="E_CLUSTER_KIND")

    async def _deploy(self, entry: dict[str, Any]) -> None:
        model, commit = str(entry["model"]), str(entry["commit_sha"])
        manifest = manifests.Manifest.model_validate(entry["manifest"])
        if self.models.by_sha(model, commit) is None:
            tar = self._scratch(".tar")
            try:
                headers = await self._fetch(f"/cluster/v1/versions/{model}/{commit}.tar", tar)
                arch = headers.get("Rrotor-Arch")
                if arch != self.arch:
                    raise ArchError(f"The head is {arch} and this node {self.arch}: one cluster is one architecture.")
                self._unpack(tar, {"src": "src", "venv": "venv"}, self.models.version_root(model, commit))
            finally:
                tar.unlink(missing_ok=True)
        found = self.models.record(model, int(entry["version"]), commit, manifest)
        self.models.activate(found)

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

    async def install(self) -> int:
        """`snapshot` from the head: fetched, installed, and the log begun after its seq. Answers the seq."""
        async with self.lock:
            tar = self._scratch(".tar")
            try:
                await self._fetch("/cluster/v1/snapshot", tar)
                seq = await asyncio.to_thread(self._install, tar)
            except (RrotorError, OSError, ValueError, KeyError, sqlite3.Error, tarfile.TarError) as err:
                self._failed({"seq": None, "kind": "snapshot"}, err)
                assert self.failure is not None  # noqa: S101  (just set)
                raise RrotorError(str(err), code=str(self.failure["code"]), status=503) from None
            finally:
                tar.unlink(missing_ok=True)
            self.log.reset(seq)
            self.applied = self.heard = seq
            self.saved(seq)
            self.failure, self.synced, self.behind_since = None, True, None
            log.info("cluster: snapshot installed", extra={"seq": seq})
            return seq

    def _install(self, tar: Path) -> int:
        with tarfile.open(tar) as t:
            member = t.extractfile("snapshot.json")
            about = json.loads(member.read() if member is not None else b"")  # a folder of that name: not JSON
        if about.get("arch") != self.arch:
            raise ArchError(
                f"The head is {about.get('arch')} and this node {self.arch}: one cluster is one architecture."
            )
        models: dict[str, Any] = about["models"]
        for name, described in models.items():
            keep = set()
            for v in described["versions"]:
                sha = str(v["commit_sha"])
                keep.add(sha)
                if self.models.by_sha(name, sha) is None:
                    prefix = f"versions/{name}/{sha}"
                    self._unpack(
                        tar, {f"{prefix}/src": "src", f"{prefix}/venv": "venv"}, self.models.version_root(name, sha)
                    )
                self.models.record(name, int(v["version"]), sha, manifests.Manifest.model_validate(v["manifest"]))
            for held in self.models.versions(name):
                if held.commit_sha not in keep:
                    self.models.remove(held)
            active = self.models.by_sha(name, described["active"]) if described.get("active") else None
            if active is not None:
                self.models.activate(active)
            store = self._store(name)
            with tarfile.open(tar) as t:
                copy = t.extractfile(f"stores/{name}.sqlite") if f"stores/{name}.sqlite" in t.getnames() else None
                if copy is not None:
                    store.parent.mkdir(parents=True, exist_ok=True)
                    staged = store.with_name(f".{store.name}.snapshot")
                    staged.write_bytes(copy.read())
                    os.replace(staged, store)  # a run reading the old one keeps it: the swap is atomic
        self.access.set_keys(list(about.get("keys") or []))
        if about.get("licence") is not None:
            self.licence.offer(about["licence"], "cluster")
        return int(about["seq"])
