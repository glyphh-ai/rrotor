# The Ada kernel, for the tests

`glyphh_kernel.py`, `glyphh_spec.py` and `glyphh_nsm.py` as they are in
`glyphh-ai/ada-private-archive`, branch `kernel-spec`, v3.6.0: a pinned copy
the tests build the Ada part from (`tests/fakes.py`, `ada_part`) and the
example model's tests import. The part buyers get is built from the archive
itself (`parts/ada/build.sh`). When the kernel moves on, copy the three files
again and bump the version here and in `parts/ada/part.json`.
