"""scripts/part.py: a key pair, and a folder built into a signed, hashed, deterministic tarball rrotor installs."""

from __future__ import annotations

import base64
import hashlib
import json

from rrotord.keys import public_key
from rrotord.parts import Parts
from tests.fakes import part


def test_keygen_makes_a_pair_the_other_side_reads(capsys):
    private, public = part.keygen()
    key = part.private_key(private)
    assert public_key(public).public_bytes_raw() == key.public_key().public_bytes_raw()
    assert part.main(["keygen"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("RROTOR_RELEASE_KEY=") and "RROTOR_RELEASE_PUBLIC_KEY=" in out


def test_build_writes_the_tarball_its_hash_and_its_signature_and_installs(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "ada"
    (folder / "lib").mkdir(parents=True)
    (folder / "part.json").write_text(json.dumps({"name": "ada", "version": "1.0.0", "mod": "ada", "arch": "any"}))
    (folder / "lib" / "k.py").write_text("X = 1\n")
    private, public = part.keygen()
    (tmp_path / "key").write_text(private)
    assert part.main(["build", str(folder), "--out", str(tmp_path / "out"), "--key-file", str(tmp_path / "key")]) == 0
    made = tmp_path / "out" / "ada-1.0.0-any.tar.gz"
    assert capsys.readouterr().out.strip() == str(made)
    data = made.read_bytes()
    assert (tmp_path / "out" / "ada-1.0.0-any.sha256").read_text().strip() == hashlib.sha256(data).hexdigest()
    sig = (tmp_path / "out" / "ada-1.0.0-any.sig").read_text().strip()
    store = Parts(tmp_path / "data", public_key(public), "arm64")
    installed = store.install("ada", "1.0.0", data, hashlib.sha256(data).hexdigest(), sig)
    assert (installed.path / "lib" / "k.py").read_text() == "X = 1\n"
    assert part.tarball(folder) == data  # the same folder gives the same bytes
    monkeypatch.setenv("RROTOR_RELEASE_KEY", private)
    assert part.main(["build", str(folder), "--out", str(tmp_path / "out2")]) == 0
    assert (tmp_path / "out2" / "ada-1.0.0-any.tar.gz").read_bytes() == data
    monkeypatch.delenv("RROTOR_RELEASE_KEY")
    assert part.main(["build", str(folder), "--out", str(tmp_path / "out3")]) == 2
    assert "No release key" in capsys.readouterr().err
    assert base64.b64decode(sig)
