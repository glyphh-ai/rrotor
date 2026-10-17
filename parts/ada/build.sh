#!/usr/bin/env bash
# Builds the Ada part for one architecture: the Ada kernel (glyphh_kernel.py, glyphh_spec.py, glyphh_nsm.py,
# glyphh_lexicon.py and lexicon/, the two seeds every text role reads, from glyphh-ai/ada-private-archive, branch
# kernel-spec) and numpy, under lib/, with part.json at the top; then
# scripts/part.py signs it with Glyphh's release key (RROTOR_RELEASE_KEY) into --out.
#
#   parts/ada/build.sh --arch amd64|arm64 --kernel <checkout of ada-private-archive> --out <dir>
#
# Python 3.13 and numpy's manylinux wheels: the part runs inside the base image (Debian 13, Python 3.13).
set -euo pipefail

arch="" kernel="" out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) arch="$2"; shift 2 ;;
    --kernel) kernel="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ -n "$arch" ] && [ -n "$kernel" ] && [ -n "$out" ] || { echo "usage: build.sh --arch amd64|arm64 --kernel <dir> --out <dir>" >&2; exit 2; }
case "$arch" in
  amd64) platform="manylinux_2_28_x86_64" ;;
  arm64) platform="manylinux_2_28_aarch64" ;;
  *) echo "arch is amd64 or arm64" >&2; exit 2 ;;
esac

here="$(cd "$(dirname "$0")" && pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/part/lib"
for f in glyphh_kernel.py glyphh_spec.py glyphh_nsm.py glyphh_lexicon.py; do
  cp "$kernel/$f" "$stage/part/lib/$f"
done
cp -R "$kernel/lexicon" "$stage/part/lib/lexicon"   # the two seeds: WordNet words, Moby parts of speech
cp "$kernel/LICENSE" "$stage/part/LICENSE" 2>/dev/null || true
version="$(python3 -c "import re,sys; print(re.search(r'^version = \"([^\"]+)\"', open('$kernel/pyproject.toml').read(), re.M).group(1))")"
sed -e "s/\"ARCH\"/\"$arch\"/" -e "s/\"3.8.0\"/\"$version\"/" "$here/part.json" > "$stage/part/part.json"

# numpy, as a wheel for the image's Python and this arch, unpacked into lib/ (no pip inside the sandbox).
uv pip install --quiet --target "$stage/part/lib" --python-platform "$platform" --python-version 3.13 \
  --only-binary :all: --no-deps "numpy>=1.24"
find "$stage/part/lib" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$stage/part/lib" -path "*numpy/tests*" -prune -exec rm -rf {} +   # tests are a third of the wheel

python3 "$here/../../scripts/part.py" build "$stage/part" --out "$out"
