# The Ada part

What the Ada mod installs into rrotor: the Ada kernel (`glyphh_kernel.py`,
`glyphh_spec.py`, `glyphh_nsm.py`, from `glyphh-ai/ada-private`,
branch `kernel-spec`) and numpy, under `lib/`, mounted read-only at
`/opt/parts/ada` in a model's sandbox with `lib/` on its `PYTHONPATH`. A model
whose manifest says `"needs": ["ada"]` imports them as `glyphh_spec` and
`glyphh_kernel`.

`build.sh` builds it per architecture; CI builds both and signs them with
Glyphh's release key (`scripts/part.py`), and the Ada mod's release carries
the tarballs, their SHA-256 and their signatures. rrotor checks the hash and
the signature with the release public key it was started with
(`RROTOR_RELEASE_PUBLIC_KEY`) before it unpacks anything, and installs the part
only while the plan's licence (`PUT /v1/licence`, checked with
`RROTOR_LICENCE_PUBLIC_KEY`) includes `ada`.
