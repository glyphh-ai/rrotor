"""Parts: checked (hash, signature, manifest, arch, rrotor version), unpacked beside the versions before, made current
in one rename, removed; what a run mounts and puts on its PYTHONPATH."""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from rrotord import parts as partsmod
from rrotord.errors import RrotorError
from rrotord.parts import Parts
from tests.fakes import keys, part_archive


def test_the_arch_names_are_the_images(monkeypatch):
    for machine, name in (("x86_64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64"), ("riscv64", "riscv64")):
        monkeypatch.setattr("platform.machine", lambda m=machine: m)
        assert partsmod.arch() == name
    assert Parts(__import__("pathlib").Path("/nowhere"), None).arch == partsmod.arch()


def test_a_signed_part_installs_becomes_current_and_the_next_version_replaces_it(tmp_path):
    store = Parts(tmp_path, keys().public, "arm64")
    assert store.installed() == [] and store.get("tools") is None
    data, sha, sig = part_archive(tmp_path)
    made = store.install("tools", "1.0.0", data, sha, sig)
    assert (made.info.name, made.info.version, made.info.mod) == ("tools", "1.0.0", "tools")
    assert (made.path / "lib" / "kernel.py").read_text() == "VALUE = 7\n"
    assert (tmp_path / "parts" / "tools" / "current").resolve() == made.path
    assert Parts.view(store.installed()) == [{"name": "tools", "version": "1.0.0", "mod": "tools", "arch": "arm64"}]
    assert store.resolve(["tools", "zeta"]) == ([("tools", made.path)], ["zeta"])
    assert store.env(["tools", "zeta"], "/opt/parts") == ["/opt/parts/tools/lib"]
    data2, sha2, sig2 = part_archive(tmp_path, version="1.1.0", files={"lib/kernel.py": "VALUE = 8\n"})
    newer = store.install("tools", "1.1.0", data2, sha2, sig2)
    got = store.get("tools")
    assert got is not None and got.path == newer.path and not (tmp_path / "parts" / "tools" / "1.0.0").exists()
    store.remove("tools")
    assert store.installed() == []
    with pytest.raises(RrotorError) as caught:
        store.remove("tools")
    assert caught.value.code == "E_PART_MISSING"


def test_a_part_is_refused_before_anything_is_unpacked(tmp_path):
    store = Parts(tmp_path, keys().public, "arm64")
    data, sha, sig = part_archive(tmp_path)
    for args, code in (
        ((data, "0" * 64, sig), "E_PART_HASH"),
        ((data, sha, keys().sign(b"other")), "E_PART_SIGNATURE"),
        ((data, sha, "***"), "E_SIGNATURE"),
        ((data + b"x", sha, sig), "E_PART_HASH"),
    ):
        with pytest.raises(RrotorError) as caught:
            store.install("tools", "1.0.0", *args)
        assert caught.value.code == code
    assert not (tmp_path / "parts").exists()
    with pytest.raises(RrotorError) as caught:
        Parts(tmp_path, None).install("tools", "1.0.0", data, sha, sig)
    assert caught.value.code == "E_CONFIG"
    big = Parts(tmp_path, keys().public, "arm64")
    with pytest.raises(RrotorError) as caught:
        big.check(b"x" * (partsmod.ARCHIVE_MAX + 1), sha, sig)
    assert caught.value.code == "E_TOO_BIG"


def test_the_manifest_must_fit_the_part_asked_for_this_arch_and_this_rrotor(tmp_path):
    store = Parts(tmp_path, keys().public, "arm64")
    cases = [
        (part_archive(tmp_path / "a"), ("zeta", "1.0.0"), "E_PART_MANIFEST"),
        (part_archive(tmp_path / "b"), ("tools", "2.0.0"), "E_PART_MANIFEST"),
        (part_archive(tmp_path / "c", arch="amd64"), ("tools", "1.0.0"), "E_PART_ARCH"),
        (part_archive(tmp_path / "d", rrotor="99.0.0"), ("tools", "1.0.0"), "E_PART_RROTOR"),
    ]
    for (data, sha, sig), (name, version), code in cases:
        with pytest.raises(RrotorError) as caught:
            store.install(name, version, data, sha, sig)
        assert caught.value.code == code
    data, sha, sig = part_archive(tmp_path / "e", arch="any", rrotor="0.1.0")
    assert store.install("tools", "1.0.0", data, sha, sig).info.arch == "any"


def _tar(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_a_tarball_without_a_good_part_json_or_not_a_tarball_is_refused(tmp_path):
    for raw in (
        _tar({"lib/x.py": b""}),
        _tar({"part.json": b"{"}),
        _tar({"part.json": json.dumps({"name": "tools"}).encode()}),
        b"not a tar",
    ):
        with pytest.raises(RrotorError) as caught:
            Parts.read_info(raw)
        assert caught.value.code == "E_PART_MANIFEST"


def test_a_current_that_is_broken_or_unreadable_counts_as_not_installed(tmp_path):
    store = Parts(tmp_path, keys().public, "arm64")
    data, sha, sig = part_archive(tmp_path)
    made = store.install("tools", "1.0.0", data, sha, sig)
    (made.path / "part.json").write_text("{}")
    assert store.get("tools") is None and store.installed() == []
    (tmp_path / "parts" / "stray.txt").write_text("")
    assert store.installed() == []


def test_part_json_that_is_a_folder_and_a_tarball_that_escapes_are_refused(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("part.json")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    with pytest.raises(RrotorError) as caught:
        Parts.read_info(buffer.getvalue())
    assert caught.value.code == "E_PART_MANIFEST"
    manifest = json.dumps({"name": "tools", "version": "1.0.0", "mod": "tools", "arch": "any"}).encode()
    escaping = _tar({"part.json": manifest, "../outside.py": b"X = 1\n"})
    store = Parts(tmp_path, keys().public, "arm64")
    import hashlib

    with pytest.raises(tarfile.FilterError):
        store.install("tools", "1.0.0", escaping, hashlib.sha256(escaping).hexdigest(), keys().sign(escaping))
    assert not (tmp_path / "parts" / "tools" / ".1.0.0.staging").exists() and store.get("tools") is None
