# The Ada kernel, for the tests

`glyphh_kernel.py`, `glyphh_spec.py`, `glyphh_nsm.py`, `glyphh_lexicon.py` and `lexicon/` (the two seeds every text
role reads: WordNet 3.0 words and the Moby parts of speech, 3.4 MB), copied from `glyphh-ai/ada-private`, branch
`main`, v3.9.0. The tests build the Ada part from them (`tests/fakes.py`, `ada_part`), and the examples' tests and
the benchmarks import them. The image's part is built from `ada-private` itself (`parts/ada/build.sh`). When the
kernel moves on, copy the files again and bump the version here and in `parts/ada/part.json`.
