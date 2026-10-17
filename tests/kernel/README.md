# The Ada kernel, for the tests

`glyphh_kernel.py`, `glyphh_spec.py`, `glyphh_nsm.py`, `glyphh_lexicon.py` and `lexicon/` (the two seeds every
text role reads: WordNet 3.0 words and the Moby parts of speech, 3.4 MB) as they are in
`glyphh-ai/ada-private-archive`, branch `kernel-spec`, v3.8.0: a pinned copy
the tests build the Ada part from (`tests/fakes.py`, `ada_part`) and the
example model's tests import. The part buyers get is built from the archive
itself (`parts/ada/build.sh`). When the kernel moves on, copy the files
again and bump the version here and in `parts/ada/part.json`.
