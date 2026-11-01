"""The vendors' agent CLIs into the image (docs/architecture.md, 5.7, built-in CLIs), at build, from image/clis.json.

    python3 image/clis.py <clis.json> <root> <arch>      # arch: amd64 or arm64 (the build's TARGETARCH)

For each CLI: its build for `arch` downloaded over HTTPS, its SHA-256 checked against the pin **before anything is
unpacked**, unpacked (the tar `data` filter) into <root>/usr/local/lib/rrotor-clis/<name>/ (a brotli-packed binary
unpacked with the image's node), its entry made executable and linked as <root>/usr/local/bin/<command>, and its
manifest (glyphh.json, installing from the image) written beside it for rrotord to deploy at boot. Nothing it
downloads runs here. Standard library only: it runs in the image's build stage."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

LIB = "usr/local/lib/rrotor-clis"
BIN = "usr/local/bin"
ARCHES = ("amd64", "arm64")


def manifest(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """A built-in CLI's glyphh.json: its manifest from clis.json, installing the image's binary."""
    given = dict(entry["manifest"])
    cli = {"install": {"image": f"/{BIN}/{entry['command']}"}, "command": entry["command"], **given.pop("cli")}
    return {"name": name, "kind": "cli", **given, "cli": cli}


def about(entry: dict[str, Any]) -> dict[str, str]:
    """What clis_list says of a built-in CLI beside its manifest: its vendor, title and pinned version."""
    return {"vendor": entry["vendor"], "title": entry["title"], "version": entry["version"]}


def check(data: bytes, sha256: str, url: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise SystemExit(f"{url}: SHA-256 is {actual}, not the pinned {sha256}")


def download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise SystemExit(f"{url} is not https")
    request = urllib.request.Request(url, headers={"User-Agent": "rrotor-image-build"})
    with urllib.request.urlopen(request, timeout=600) as response:  # noqa: S310  (https only, checked above)
        body: bytes = response.read()
        return body


def unpack(data: bytes, folder: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        tar.extractall(folder, filter="data")


def unbrotli(packed: Path, out: Path) -> None:
    """A brotli-packed binary unpacked with node's zlib (the image has node, not brotli)."""
    script = "const z=require('zlib'),f=require('fs');f.writeFileSync(process.argv[2],z.brotliDecompressSync(f.readFileSync(process.argv[1])))"
    subprocess.run(["node", "-e", script, str(packed), str(out)], check=True)
    packed.unlink()


def install(name: str, entry: dict[str, Any], root: Path, arch: str, fetch: Any = download) -> Path:
    """One CLI into root: answers its linked command."""
    build = entry["builds"][arch]
    data = fetch(build["url"])
    check(data, build["sha256"], build["url"])
    folder = root / LIB / name
    folder.mkdir(parents=True)
    unpack(data, folder)
    target = folder / build["entry"]
    if build.get("brotli"):
        unbrotli(folder / build["brotli"], target)
    if not target.resolve().is_relative_to(folder.resolve()) or not target.is_file():
        raise SystemExit(f"{name}: {build['entry']} is not a file in its package")
    target.chmod(0o755)
    link = root / BIN / entry["command"]
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(Path("/") / LIB / name / build["entry"])
    (folder / "glyphh.json").write_text(json.dumps(manifest(name, entry), indent=2) + "\n")
    (folder / "vendor.json").write_text(json.dumps(about(entry)) + "\n")
    return link


def main(argv: list[str]) -> int:
    pins, root, arch = Path(argv[1]), Path(argv[2]), argv[3]
    if arch not in ARCHES:
        raise SystemExit(f"arch is one of {', '.join(ARCHES)}, not {arch}")
    for name, entry in json.loads(pins.read_text())["clis"].items():
        link = install(name, entry, root, arch)
        print(f"{name} {entry['version']} ({arch}) -> /{link.relative_to(root)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
