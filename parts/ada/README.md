# The Ada part

The Ada kernel (`glyphh_kernel.py`, `glyphh_spec.py`, `glyphh_nsm.py`, `glyphh_lexicon.py` and `lexicon/`, from
`glyphh-ai/ada-private`, branch `main`) and numpy, under `lib/`. A model whose manifest says `"needs": ["ada"]` gets
it read-only at `/opt/parts/ada`, with `lib/` on its `PYTHONPATH`, and imports `glyphh_spec` and `glyphh_kernel`.

`build.sh --arch amd64|arm64 --kernel <checkout> --out <dir>` builds it and signs it with Glyphh's release key
(`scripts/part.py`, `RROTOR_RELEASE_KEY`). CI builds both architectures on main, publishes them as the release
`part-ada-<kernel version>`, and builds them into the published image, which installs this architecture's at boot.
rrotor checks the hash and the signature with `RROTOR_RELEASE_PUBLIC_KEY` before it unpacks anything, and runs a
model that needs it only while the licence includes `ada` (`docs/architecture.md`, sections 9 and 10).
