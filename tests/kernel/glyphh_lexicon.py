"""Ada's own reading: the English a text role reads with no language model in the path. Two seeds ship with the
kernel, in `lexicon/` beside this file (or `GLYPHH_LEXICON_DIR`):

  words.jsonl.gz    WordNet 3.0, 77,817 single words: each its synonyms, its near words (hypernyms, hyponyms,
                    similar-to, also-see, attributes, pertainyms, derivations) and its part of speech
  grammar.jsonl.gz  the Moby Part-of-Speech list, 192,960 word forms: each its parts of speech

A word's glyph is its own symbol tilted toward its neighbourhood: itself at 1.0, its synonyms sharing SYNONYMS,
its near words sharing NEAR, its classes sharing CLASSES, bundled by weight. So "arrive" and "come" correlate,
"stolen" leans toward "taken", and two nouns lean toward each other a little; a word the seeds do not know is its
symbol alone, as before. The kernel reads every item of a weighted set this way (a text role's bag, since the spec
emits one), nothing else: a category's value is never a word. Licences: lexicon/LICENSES.md."""

from __future__ import annotations

import gzip
import json
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

SYNONYMS = 0.9  # the voice a word's synonyms share in its glyph
NEAR = 0.6  # its near words'
CLASSES = 0.3  # its parts of speech'
CACHE = 50000  # word glyphs kept, per (seed, dim)


class Lexicon:
    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or Path(os.environ.get("GLYPHH_LEXICON_DIR") or Path(__file__).parent / "lexicon")
        self.enabled = True
        self._words: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] | None = None
        self._grammar: dict[str, tuple[str, ...]] | None = None
        self._glyphs: OrderedDict[tuple[str, int, int], np.ndarray] = OrderedDict()

    @property
    def available(self) -> bool:
        return (self.folder / "words.jsonl.gz").is_file() or (self.folder / "grammar.jsonl.gz").is_file()

    def _load(self) -> None:
        if self._words is not None:
            return
        self._words, self._grammar = {}, {}
        path = self.folder / "words.jsonl.gz"
        if path.is_file():
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)["situation"]["word"]
                    m = r["meaning"]
                    self._words[r["identity"]["lemma"]] = (tuple(m.get("synonyms", ())), tuple(m.get("near", ())), m["pos"])
        path = self.folder / "grammar.jsonl.gz"
        if path.is_file():
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)["situation"]["word"]
                    self._grammar[r["identity"]["form"]] = tuple(r["grammar"]["pos"])

    def entry(self, word: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
        """A word's synonyms, near words and classes, or None when neither seed knows it."""
        self._load()
        assert self._words is not None and self._grammar is not None
        found = self._words.get(word)
        classes = self._grammar.get(word) or ((found[2],) if found else ())
        if found is None and not classes:
            return None
        return (found[0] if found else ()), (found[1] if found else ()), tuple(classes)

    def size(self) -> tuple[int, int]:
        self._load()
        assert self._words is not None and self._grammar is not None
        return len(self._words), len(self._grammar)

    def forget(self) -> None:
        self._glyphs.clear()

    def glyph(self, word: str, seed: int, dim: int) -> np.ndarray:
        """The word's glyph through the seeds (cached), or its bare symbol when off, absent or unknown."""
        from glyphh_kernel import bundle, symbol

        if not self.enabled or not self.available:
            return symbol(f"val:{word}", seed, dim)
        key = (word, seed, dim)
        held = self._glyphs.get(key)
        if held is not None:
            self._glyphs.move_to_end(key)
            return held
        entry = self.entry(word)
        own = symbol(f"val:{word}", seed, dim)
        if entry is None:
            made = own
        else:
            synonyms, near, classes = entry
            parts, weights = [own], [1.0]
            for items, share, prefix in ((synonyms, SYNONYMS, "val"), (near, NEAR, "val"), (classes, CLASSES, "class")):
                for item in items:
                    parts.append(symbol(f"{prefix}:{item}", seed, dim))
                    weights.append(share / len(items))
            made = bundle(parts, weights)
        self._glyphs[key] = made
        if len(self._glyphs) > CACHE:
            self._glyphs.popitem(last=False)
        return made

    def view(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "available": self.available, "folder": str(self.folder)}


LEXICON = Lexicon()
