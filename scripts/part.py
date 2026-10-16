#!/usr/bin/env python3
"""Build and sign a part: a folder with part.json at its top becomes <name>-<version>-<arch>.tar.gz beside
<...>.sha256 and <...>.sig (Ed25519 over the tarball, by Glyphh's release key), which is what a mod's release carries
and rrotor checks before it unpacks (rrotord/parts.py). The private key is base64 of its raw 32 bytes, in the
environment (RROTOR_RELEASE_KEY) or a file; `keygen` makes a pair.

    python scripts/part.py keygen                      prints a private and a public key (base64)
    python scripts/part.py build <folder> --out <dir>  the tarball, its SHA-256 and its signature

Deterministic: the tarball's entries are sorted and carry no owner or time, so the same folder gives the same
hash."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def keygen() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(key.private_bytes_raw()).decode()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return private, public


def private_key(text: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(text.strip()))


def tarball(folder: Path) -> bytes:
    """The folder as a gzipped tar, the same bytes for the same files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9, format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(p for p in folder.rglob("*")):
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(folder)))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if path.is_file():
                with path.open("rb") as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)
    return buffer.getvalue()


def build(folder: Path, out: Path, key: Ed25519PrivateKey) -> Path:
    info = json.loads((folder / "part.json").read_text())
    data = tarball(folder)
    stem = f"{info['name']}-{info['version']}-{info['arch']}"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}.tar.gz").write_bytes(data)
    (out / f"{stem}.sha256").write_text(hashlib.sha256(data).hexdigest() + "\n")
    (out / f"{stem}.sig").write_text(base64.b64encode(key.sign(data)).decode() + "\n")
    return out / f"{stem}.tar.gz"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keygen")
    b = commands.add_parser("build")
    b.add_argument("folder", type=Path)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--key-file", type=Path, help="the private key; else RROTOR_RELEASE_KEY in the environment")
    args = parser.parse_args(argv)
    if args.command == "keygen":
        private, public = keygen()
        print(f"RROTOR_RELEASE_KEY={private}\nRROTOR_RELEASE_PUBLIC_KEY={public}")
        return 0
    text = args.key_file.read_text() if args.key_file else os.environ.get("RROTOR_RELEASE_KEY", "")
    if not text.strip():
        print("No release key: --key-file, or RROTOR_RELEASE_KEY in the environment.", file=sys.stderr)
        return 2
    made = build(args.folder, args.out, private_key(text))
    print(made)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
