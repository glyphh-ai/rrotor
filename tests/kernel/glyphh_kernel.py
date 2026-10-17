"""
Ada kernel — the AI-native kernel. Single file, numpy + stdlib only.

This is software whose user is a model. There is no UI, no service, no
config. If you are an agent reading this: call spec() — or read SPEC
below — before using the kernel. The file carries its own manual so
the guidance can never be separated from the code.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from collections.abc import Callable

DIM = 4096  # default dimensionality; ~1e-3 cross-talk between random symbols
TAU = 0.12  # default softmax temperature (fit on Kubernetes issues; calibrate())
POOL = 25  # nearest precedents a probability is spread over
TAUS = (0.01, 0.015, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2, 0.3, 0.5, 0.7, 1.0, 1.5)
ENCODING = 3  # bump when encode() changes; stores re-encode on open

SPEC = """\
Ada kernel — substrate guidance for the agent using it

WHAT THIS IS. Your state and execution layer beyond the context
window. Contexts are mortal; the SQLite files a Space writes are not.
Record graded experience as you work; the next context (yours or
another agent's) reopens the files and inherits ground truth instead
of prose about it.

THE FIVE PRIMITIVES. All glyphs are bipolar +/-1 vectors of one fixed
dimension (default 4096); every operation returns the type it takes,
so composites nest indefinitely.
  symbol(key, seed=42, dim)  deterministic glyph: same inputs -> the
      identical vector on any machine, forever. Shared seed = shared
      vocabulary between agents; different seeds are mutual noise.
  bind(a, b)     associate (elementwise multiply). Self-inverse; the
      result resembles neither input. Use for role-filler pairs.
  bundle(parts, weights=None)  superpose (majority vote); resembles
      ALL inputs. Order-free: the same parts in any order give the same
      glyph. weights (one per part, >= 0) give each part its voice in
      the vote; the result is a glyph, so weighted bundles nest and
      weights live at every level. Capacity is finite: past a few dozen
      parts detail fades and gist survives — that is abstraction; keep
      receipts for the detail.
  roll(v, k)     rotate: order, position, depth. Undo with roll(v,-k).
  sim(a, b)      cosine in [-1, 1]. Random glyphs sit near 0. For
      situations, sim tracks the FRACTION OF SHARED ATTRIBUTE PAIRS,
      whichever attributes they are (measured, 4 attributes: sharing
      1/2/3 -> 0.14/0.28/0.44; 2 attributes sharing 1 -> 0.25), even
      when the shared attributes are decision-irrelevant.

DATA. encode(data, weights) is one glyph for any JSON-shaped value:
  an object is a weighted bundle of bind(attr:key, child), an array a
  set of its items, a scalar the symbol of its text; nested children
  roll once so paths stay apart. weights mirror the data —
  {"risk": 1.0, "plan": {"*": 0.5, "step": 2}}: a number weighs a key,
  an object weighs inside it with its own weight under "*"; unlisted
  keys weigh 1.0. explain(query, record, weights) -> Fact: the fact
  tree — per key, its score (exact weighted sim of that subtree), its
  weight, its share of the parent (shares sum to the parent's score),
  and the glyph sim beside it. root.score is the weighted sim. A key on
  one side only scores 0 at full weight: weigh it 0 if its absence
  should not count. Weights are how you say which attributes decide;
  that is rule 2, made explicit.

THE TWO CONSTRUCTS.
  Strand: ordered meaning. append() glyphs; state(decay) folds the
      sequence into one glyph where recent items dominate and order
      matters. Detail recoverable a few steps back; the KIND of
      trajectory recognizable much longer.
  Space(dim, k, min_sim, seed, decay, author, path): graded precedent.
      record(situation, outcome=None, events=None, ts=None) — one write,
      permanent, time-stamped. With no outcome the row is memory: it
      shows in facts() but never votes and never takes a probability. A situation is an object; values may
      nest (see DATA). Every read below takes weights=: weighted, it
      ranks and gates on the EXACT weighted sim (the fact-tree score),
      so min_sim means 'the weighted share of attributes that must
      agree' and a weight can raise a match, not only lower it.
      query(...) -> Answer(sufficient, confidence, support, count,
      top, outcomes, receipts); only precedents clearing min_sim vote.
      answers(...) -> [Evidence] for EVERY outcome with qualifying
      precedent: the read for situations with more than one true
      outcome. evidence(situation, outcome) grades one candidate.
      matches(situation, outcome) — max sim among precedents with THAT
      outcome: the veto primitive, undilutable by consensus.
      facts(situation, weights=None, top=k) -> [FactTree]: every
      precedent scored exactly, the best `top` explained as fact trees
      (events as the key "~events"), each with its glyph sim and its
      citation, and probability: P(that outcome | situation), a
      softmax at temperature tau over the POOL (25) nearest. The read
      to show WHY, not just what.
      probabilities(situation, weights=None) -> {outcome: P}.
      calibrate(sample=100, weights=None) fits tau by holding out the
      newest precedents one at a time; saved in the file. Until then
      tau is TAU (0.12). Calibrated, "act at P >= 0.9" means right
      about 9 times in 10 — on the Kubernetes test it took confident
      answers from 87% to 94% right.
      refresh() pulls rows other agents wrote to a shared WAL file;
      merge(other_path) unions another Space file by id, authors kept.

THE CONTRACT: NOTHING UNGRADED. Route every domain decision through
decide(wins, vetoes, situation, events) -> Decision. It is:
  a = wins.query(situation, events)
  if a.sufficient and a.confidence >= 0.5 and a.count >= 3:
      if not vetoed(a.top):               # see rule 3
          act(a.top)                      # reflex: free, cited
      else: consult fallback              # vetoed
  else: consult fallback                  # insufficient precedent
  success = the WORLD grades the action   # never self-graded
  (wins if success else vetoes).record(situation, action, events)
Fallback chain by cost: reflex (free) -> consultant (paid) -> human
(last resort; the answer is itself a grade). Consult rate should fall
over the Space's life — that curve is the product metric.

RULES THAT DECIDE WHETHER IT WORKS.
  1. Trust needs three gates: sufficient (similarity), confidence
     (consensus), count (population). A consensus of one is
     confidence 1.0 and means almost nothing — gate count >= 3.
  2. Attribute hygiene: encode ONLY decision-relevant attributes;
     incidental ones (ids, regions, timestamps) poison neighborhoods.
     Size min_sim as the attribute-overlap fraction that should count
     as 'the same kind of situation'.
  3. Vetoes suppress, they don't erase, and the newest evidence wins:
     a reflex is blocked while a recorded failure sits as close as the
     wins recommending it AND is newer than the newest such win. A
     later reproduction lifts an older veto; a later failure
     re-imposes it. After drift, expect a few wrong firings while
     failures arrive. Use matches(), never a consensus vote, for
     vetoes. decide(min_veto_count=2) demands corroborated failures:
     harder to sabotage, slower to stop.
  4. One question, several true answers: when a situation genuinely
     has many outcomes (a file imports five modules), confidence
     splits across them and no single one clears 0.5. That is not
     disagreement. Read answers() and gate each outcome on its own
     count instead of on its share.
  5. Precedent never overrides a proof: auth, money conservation,
     state-machine legality stay hand-written.

MORE THAN ONE AGENT. Private kernel per agent; one shared kernel per
team, author= set so receipts show who grounded what. Share a file
live (WAL + refresh) or ship and merge(). Verify before you trust:
replay incoming receipts against your own world before merging.
Meaning is operational at every scale: a concept is meaningful exactly
to the degree its glyph's neighborhood predicts the world; a
teammate's precedent is trustworthy exactly to the degree its
receipts replay.

STORES OUTLIVE ENCODINGS. Every file records the encoding it was
written under. Opening (or merging) a file from an older encoding
re-encodes its glyphs from the stored situations and events, so old
precedent keeps matching fresh queries. Events are stored with each
precedent, so re-encoding is lossless (rows written before events
were stored re-encode without their trajectory).
"""


def spec() -> str:
    """The substrate guidance. An agent that imports this kernel cold
    should read spec() before recording or querying anything."""
    return SPEC


def reencode_store(
    path: str, dim: int = DIM, k: int = 15, min_sim: float = 0.10, seed: int = 42, decay: float = 0.7
) -> int:
    """Re-encode every precedent's glyph from its stored situation and
    events, under the current encoding, and stamp the file with it.
    Opening a Space does this automatically for files from an older
    encoding; call it directly to force a rebuild (e.g. after changing
    seed or decay). Returns rows re-encoded.

    Rows recorded before events were persisted re-encode without their
    trajectory component."""
    db = sqlite3.connect(str(Path(path).expanduser()))
    _ensure_schema(db)
    space = Space(dim=dim, k=k, min_sim=min_sim, seed=seed, decay=decay)
    n = _reencode_rows(db, space)
    db.commit()
    db.close()
    return n


# ── primitives ──────────────────────────────────────────────────────


def symbol(key: str, seed: int = 42, dim: int = DIM) -> np.ndarray:
    """Deterministic bipolar (+1/-1) symbol for a key.

    Same (key, seed, dim) -> identical vector, on any machine, forever.
    Shared seeds are shared vocabulary; different seeds are mutually
    random noise. This is how two agents agree on meaning.
    """
    cached = _SYMBOLS.get((key, seed, dim))
    if cached is None:
        digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
        rng = np.random.RandomState(np.frombuffer(digest[:4], dtype=np.uint32))
        cached = rng.randint(0, 2, dim, dtype=np.int8) * 2 - 1
        _SYMBOLS[(key, seed, dim)] = cached
    return cached.copy()


_SYMBOLS: dict[tuple[str, int, int], np.ndarray] = {}


def bind(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Associate two glyphs (elementwise multiply). Self-inverse:
    bind(bind(a, b), b) == a. The result resembles neither input."""
    return (a * b).astype(np.int8)


def _tiebreak(parts: Sequence[np.ndarray]) -> np.ndarray:
    """Tie-break vector for a majority vote: a pseudo-random glyph
    seeded by the hash of the parts as a SET (their bytes, sorted).

    Why this shape. A shared global tie-break vector injects a common
    component into every even-count bundle, so unrelated small
    situations look alike. Deriving it from one part (encoding 1 used
    roll(parts[0])) made similarity depend on which attribute differed:
    changing the attribute that sorts first cost twice as much as
    changing the one that sorts last (0.26 vs 0.50 at two attributes).
    A hash of the whole multiset is deterministic, different for every
    distinct input, uncorrelated with any single part, and independent
    of part order — so tied positions carry no bias at all."""
    h = hashlib.sha256()
    for b in sorted(np.ascontiguousarray(p).tobytes() for p in parts):
        h.update(b)
    rng = np.random.RandomState(np.frombuffer(h.digest()[:4], dtype=np.uint32))
    return rng.randint(0, 2, len(parts[0]), dtype=np.int8) * 2 - 1


def bundle(parts: Sequence[np.ndarray], weights: Sequence[float] | None = None) -> np.ndarray:
    """Superpose glyphs (majority vote). The result resembles ALL
    inputs. Capacity is finite: past a few dozen items, individual
    contributions fade — detail dies, gist survives. That is
    abstraction, not failure; keep receipts if you need the detail.

    weights: one per part, finite and >= 0; a part's weight is its
    voice in the vote (0 drops it). The result is still a glyph, so a
    weighted bundle nests inside another — weights live at every level.
    The weighted vote sums in a canonical order (parts by their bytes),
    so it stays order-free and is bit-identical across machines."""
    if not parts:
        raise ValueError("cannot bundle nothing")
    if weights is None:
        total = np.sum(np.stack(parts).astype(np.int32), axis=0)
        signs = np.sign(total)
        ties = signs == 0
        if ties.any():
            signs[ties] = _tiebreak(parts)[ties]
        return signs.astype(np.int8)
    if len(weights) != len(parts):
        raise ValueError(f"{len(parts)} parts but {len(weights)} weights")
    pairs = [(np.ascontiguousarray(p), _check_weight(w)) for p, w in zip(parts, weights)]
    pairs = sorted(((p, w) for p, w in pairs if w > 0), key=lambda pw: (pw[0].tobytes(), pw[1]))
    if not pairs:
        raise ValueError("cannot bundle when every weight is 0")
    acc = np.zeros(len(pairs[0][0]), dtype=np.float64)
    for p, w in pairs:
        acc += w * p.astype(np.float64)
    signs = np.sign(acc)
    ties = signs == 0
    if ties.any():
        signs[ties] = _tiebreak([p for p, _ in pairs])[ties]
    return signs.astype(np.int8)


def _check_weight(w: Any) -> float:
    if isinstance(w, bool) or not isinstance(w, (int, float)):
        raise TypeError("weight must be a number")
    if not np.isfinite(w) or w < 0:
        raise ValueError(f"weight must be finite and >= 0, got {_text(w)}")
    return float(w)


def roll(v: np.ndarray, k: int = 1) -> np.ndarray:
    """Rotate a glyph. Encodes order/position/depth: roll(v, 1) is
    'v, one step ago'. Orthogonal to v; invert with roll(v, -k)."""
    return np.roll(v, k)


def sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity for bipolar glyphs, in [-1, 1]. Two random
    symbols sit near 0. For situation glyphs, sim approximately tracks
    the FRACTION OF SHARED ATTRIBUTE PAIRS — two 4-attribute
    situations sharing 3 attributes measure ~0.44, not ~0."""
    return float(np.dot(a.astype(np.float32), b.astype(np.float32))) / len(a)


# ── data: structure in, glyph out; weighted sim, fact trees back ────
#
# Any JSON-shaped value encodes. An object is a weighted bundle of
# bind(attr:key, child); an array is a bundle of its items (a set:
# order belongs in a Strand); a scalar is the symbol of its text. A
# nested object or array is rolled once before it binds, so
# {"a": {"b": x}} and {"b": {"a": x}} stay apart. A flat object of
# strings encodes exactly as a Space situation always has.
#
# Weights mirror the data: {"risk": 1.0, "plan": {"*": 0.5, "step": 2}}.
# A number weighs that key; an object weighs inside it, its own weight
# under "*". Unlisted keys weigh 1.0; weights for absent keys are
# ignored.


def _text(v: Any) -> str:
    """A scalar's text, the same in every language: strings as they
    are, true/false/null, numbers as JavaScript prints them."""
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) and abs(v) < 10**21:
        return str(v)
    if isinstance(v, (int, float)):
        return _js_number(float(v))
    raise TypeError(f"cannot encode a {type(v).__name__}: {v!r}")


def _js_number(x: float) -> str:
    """Number.prototype.toString for a double: shortest round-trip
    digits, exponent outside [1e-7, 1e21)."""
    if x != x:
        return "NaN"
    if x in (float("inf"), float("-inf")):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    mantissa, _, exp = repr(abs(x)).partition("e")
    whole, _, frac = mantissa.partition(".")
    digits = (whole + frac).lstrip("0")
    point = len(whole) + (int(exp) if exp else 0)  # digits are 0.d * 10^point
    point -= len(whole + frac) - len((whole + frac).lstrip("0"))
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= point <= 21:
        return sign + digits + "0" * (point - k)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * -point + digits
    e = point - 1
    e_text = ("+" if e >= 0 else "-") + str(abs(e))
    if k == 1:
        return f"{sign}{digits}e{e_text}"
    return f"{sign}{digits[0]}.{digits[1:]}e{e_text}"


def _weight(w: Any) -> tuple[float, dict[str, Any] | None]:
    """(own weight, weights inside) for one entry of a weights tree."""
    if w is None:
        return 1.0, None
    if isinstance(w, dict):
        return _check_weight(w.get("*", 1.0)), w
    return _check_weight(w), None


def _keyed(
    data: dict[str, Any], weights: dict[str, Any] | None, seed: int, dim: int
) -> tuple[list[np.ndarray], list[float]]:
    """bind(attr:key, child) for every key, code-point order, with
    each key's weight."""
    parts, ws = [], []
    for key in sorted(data):
        if not isinstance(key, str):
            raise TypeError(f"object keys must be strings, got {key!r}")
        own, inner = _weight(weights.get(key) if weights else None)
        value = data[key]
        if own == 0 or _weighs_nothing(value, inner):
            continue  # it would carry no voice in the vote
        child = _encode(value, inner, seed, dim)
        if isinstance(value, (dict, list, tuple)):
            child = roll(child, 1)
        parts.append(bind(symbol(f"attr:{key}", seed, dim), child))
        ws.append(own)
    return parts, ws


def _weighted_set(data: Any) -> bool:
    """A list of [scalar, weight] pairs: a set whose items weigh differently (a bag of words whose words were
    learned, for one). A plain set is a list of scalars; a list of pairs of anything else is a set of pairs."""
    return all(
        isinstance(x, (list, tuple))
        and len(x) == 2
        and isinstance(x[0], (str, int, float))
        and not isinstance(x[0], bool)
        and isinstance(x[1], (int, float))
        and not isinstance(x[1], bool)
        and 0 <= x[1] <= 1
        for x in data
    )


def _encode(data: Any, weights: dict[str, Any] | None, seed: int, dim: int) -> np.ndarray:
    if isinstance(data, dict):
        if not data:
            raise ValueError("cannot encode an empty object")
        parts, ws = _keyed(data, weights, seed, dim)
        if not parts:
            raise ValueError("cannot bundle when every weight is 0")
        return bundle(parts, ws)
    if isinstance(data, (list, tuple)):
        if not data:
            raise ValueError("cannot encode an empty array")
        if _weighted_set(data):  # [[item, weight], ...]: each item's voice in the vote is its weight
            parts = [symbol(f"val:{_text(item)}", seed, dim) for item, _ in data]
            weights = [float(w) for _, w in data]
            return bundle(parts, weights) if sum(weights) > 0 else bundle(parts)
        return bundle([_encode(x, None, seed, dim) for x in data])
    return symbol(f"val:{_text(data)}", seed, dim)


def encode(data: Any, weights: dict[str, Any] | None = None, seed: int = 42, dim: int = DIM) -> np.ndarray:
    """One glyph for any JSON-shaped value — objects nest, arrays are
    sets, scalars are symbols — with weights at every level (see the
    note above). The glyph is what sim() compares; explain() gives the
    exact weighted sim of two values as a fact tree."""
    return _encode(data, weights, seed, dim)


_ABSENT = object()


def _2(x: float) -> str:
    return f"{round(x, 2) + 0.0:.2f}"  # no "-0.00"


def _4(x: float) -> float:
    return round(x, 4) + 0.0  # no -0.0


@dataclass
class Fact:
    """One node of a fact tree: how one part of a query compares with
    the same part of a record.

    score: the exact weighted sim of this subtree — the weighted mean of
        the children's scores, or the substrate sim at a leaf.
    share: weight * score / the siblings' total weight; the shares of a
        node's children sum to its score, so the tree adds up.
    sim: the substrate sim of the two encoded subtrees — what a glyph
        sees of this node, noise and all.
    status: 'both', 'query_only' or 'record_only'."""

    key: str
    weight: float
    score: float
    share: float
    sim: float
    status: str
    query: Any
    record: Any
    children: list[Fact]

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "weight": self.weight,
            "score": _4(self.score),
            "share": _4(self.share),
            "sim": _4(self.sim),
            "status": self.status,
        }
        if not self.children:
            if self.query is not _ABSENT:
                out["query"] = self.query
            if self.record is not _ABSENT:
                out["record"] = self.record
        else:
            out["children"] = [c.to_json() for c in self.children]
        return out

    def to_text(self, depth: int = 0) -> str:
        pad = "  " * depth
        if self.children:
            head = f"{pad}{self.key or '.'}"
        elif self.status == "both":
            head = f"{pad}{self.key} = {json.dumps(self.record, ensure_ascii=False)}"
            if self.query != self.record:
                head += f" (query {json.dumps(self.query, ensure_ascii=False)})"
        else:
            missing = "record" if self.status == "query_only" else "query"
            head = f"{pad}{self.key}: not in the {missing}"
        line = f"{head}  score {_2(self.score)} × w {self.weight:g} → {_2(self.share)}"
        return "\n".join([line] + [c.to_text(depth + 1) for c in self.children])


def _tally(children: list[Fact]) -> float:
    """Each child's share of its parent; returns the parent's score."""
    total = sum(c.weight for c in children)
    for c in children:
        c.share = c.weight * c.score / total if total else 0.0
    return sum(c.share for c in children)


def _weighs_nothing(data: Any, weights: dict[str, Any] | None) -> bool:
    """True when every key of an object weighs 0 here, all the way down."""
    if not isinstance(data, dict) or not weights:
        return False
    for k, v in data.items():
        own, inner = _weight(weights.get(k))
        if own > 0 and not _weighs_nothing(v, inner):
            return False
    return True


def _side_by_side(q: Any, r: Any, weights: dict[str, Any] | None, seed: int, dim: int) -> float:
    """The glyph sim of two subtrees under the same weights; 0 when either
    side weighs nothing there (there is no glyph to compare)."""
    if _weighs_nothing(q, weights) or _weighs_nothing(r, weights):
        return 0.0
    return sim(_encode(q, weights, seed, dim), _encode(r, weights, seed, dim))


def _explain(
    key: str, weight: float, q: Any, r: Any, weights: dict[str, Any] | None, seed: int, dim: int, glyphs: bool = True
) -> Fact:
    """glyphs=False skips the glyph sim at inner nodes: the score is
    the same, only the side-by-side sim is left 0 (scoring every row
    of a store needs the score alone)."""
    if q is _ABSENT or r is _ABSENT:
        side = r if q is _ABSENT else q
        own = 0.0 if _weighs_nothing(side, weights) else weight  # nothing weighed under it: nothing missing
        return Fact(key, own, 0.0, 0.0, 0.0, "record_only" if q is _ABSENT else "query_only", q, r, [])
    if _weighs_nothing(q, weights) or _weighs_nothing(r, weights):
        return Fact(key, 0.0, 0.0, 0.0, 0.0, "both", q, r, [])  # nothing to compare on one side: out of the mean
    if not (isinstance(q, dict) and isinstance(r, dict)):
        leaf = _side_by_side(q, r, weights, seed, dim)
        return Fact(key, weight, leaf, 0.0, leaf, "both", q, r, [])
    children = []
    for k in sorted(set(q) | set(r)):
        own, inner = _weight(weights.get(k) if weights else None)
        children.append(_explain(k, own, q.get(k, _ABSENT), r.get(k, _ABSENT), inner, seed, dim, glyphs))
    glyph_sim = _side_by_side(q, r, weights, seed, dim) if glyphs else 0.0
    return Fact(key, weight, _tally(children), 0.0, glyph_sim, "both", q, r, children)


def explain(query: Any, record: Any, weights: dict[str, Any] | None = None, seed: int = 42, dim: int = DIM) -> Fact:
    """The fact tree for one query against one record: per key, how
    close, with what weight, and its share of the whole. root.score is
    the weighted sim. A key on one side only scores 0 at full weight,
    so give it weight 0 if its absence shouldn't count."""
    root = _explain("", 1.0, query, record, weights, seed, dim)
    root.share = root.score
    return root


# ── Strand: ordered meaning ─────────────────────────────────────────


class Strand:
    """A sequence of glyphs whose ORDER is part of the meaning.

    state() folds the sequence into one glyph: each codon is rotated
    by its age and decayed, so recent items dominate and order is
    preserved — two strands with the same items in different order
    have different states. Detail survives ~3 steps; the gist of
    'what kind of trajectory is this' survives far longer.
    """

    def __init__(self, codons: list[np.ndarray] | None = None):
        self.codons: list[np.ndarray] = list(codons or [])

    def append(self, codon: np.ndarray) -> Strand:
        self.codons.append(codon)
        return self

    def state(self, decay: float = 0.7) -> np.ndarray:
        if not self.codons:
            raise ValueError("empty strand has no state")
        acc = np.zeros(len(self.codons[0]), dtype=np.float64)
        rolled = []
        for age, codon in enumerate(reversed(self.codons)):
            shifted = roll(codon, age)
            rolled.append(shifted)
            acc += (decay**age) * shifted.astype(np.float64)
        signs = np.sign(acc)
        ties = signs == 0
        if ties.any():
            signs[ties] = _tiebreak(rolled)[ties]
        return signs.astype(np.int8)

    def __len__(self) -> int:
        return len(self.codons)


# ── Space: graded precedent ─────────────────────────────────────────


@dataclass
class Answer:
    """What a Space says. Trust `top` only when `sufficient` is True
    AND `confidence` clears your threshold AND `count` is more than a
    couple — a consensus of one is not consensus. receipts = the
    precedents behind the answer: (id, similarity, outcome, hash)."""

    sufficient: bool
    confidence: float  # consensus among qualifying precedents
    support: float  # how CLOSE they are (mean similarity)
    count: int  # how many precedents cleared min_sim
    top: str | None  # leading outcome, None if insufficient
    outcomes: dict[str, float]  # similarity-weighted distribution
    receipts: list[tuple[str, float, str | None, str, str]]  # +author

    def to_json(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "confidence": round(self.confidence, 4),
            "support": round(self.support, 4),
            "count": self.count,
            "top": self.top,
            "outcomes": {k: round(v, 4) for k, v in self.outcomes.items()},
            "receipts": [
                {"id": i, "sim": round(s, 4), "outcome": o, "hash": h, "author": a} for i, s, o, h, a in self.receipts
            ],
        }


@dataclass
class Evidence:
    """How well one outcome is supported in one situation, judged on
    its own — not as a share of a vote. `count` precedents with this
    outcome clear min_sim, at mean similarity `support` (nearest:
    `nearest`); the newest of them was recorded at `latest` (epoch
    seconds, 0.0 for rows written before time-stamping)."""

    outcome: str
    count: int
    support: float
    nearest: float
    latest: float
    authors: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "count": self.count,
            "support": round(self.support, 4),
            "nearest": round(self.nearest, 4),
            "latest": self.latest,
            "authors": self.authors,
        }


@dataclass
class FactTree:
    """One precedent explained against a situation (Space.facts()).
    score = root.score, the exact weighted sim; sim = the glyph sim
    that found it; probability = P(this precedent's outcome | the
    situation), calibrated (see Space.calibrate()); the rest cites the
    precedent."""

    id: str
    outcome: str | None
    author: str
    ts: float
    hash: str
    sim: float
    root: Fact
    probability: float = 0.0

    @property
    def score(self) -> float:
        return self.root.score

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "outcome": self.outcome,
            "probability": _4(self.probability),
            "score": _4(self.score),
            "sim": _4(self.sim),
            "author": self.author,
            "ts": self.ts,
            "hash": self.hash,
            "tree": self.root.to_json(),
        }

    def to_text(self) -> str:
        return (
            f"{self.outcome or '(memory)'}  p {_2(self.probability)}  score "
            f"{_2(self.score)}  (sim "
            f"{_2(self.sim)}, {self.id} by {self.author})\n" + self.root.to_text(1)
        )


class Space:
    """Precedent memory: record graded experience, answer by similarity.

    The contract that makes this safe for a machine caller:
      - record() is one write, permanent, no training run
      - query() returns calibrated confidence over the precedents that
        clear min_sim — rows below the bar carry no vote
      - every answer cites the precedents it stands on
      - with nothing above min_sim the Space says 'insufficient'
        instead of guessing

    Encode ONLY decision-relevant attributes: sim tracks attribute
    overlap, so incidental attributes (region, timestamps, ids) make
    unrelated situations look alike and poison neighborhoods. Set
    min_sim to the overlap fraction that should count as 'the same
    kind of situation' — with a 4-attribute situation, requiring 3
    shared attributes means min_sim ~0.4 (sharing 2 measures ~0.28),
    and the default 0.10 only excludes near-strangers.
    """

    def __init__(
        self,
        dim: int = DIM,
        k: int = 15,
        min_sim: float = 0.10,
        seed: int = 42,
        decay: float = 0.7,
        author: str = "anon",
        path: str | None = None,
        tau: float | None = None,
    ):
        self.dim, self.k, self.min_sim, self.seed = dim, k, min_sim, seed
        # temperature for probabilities: given, else the file's
        # calibrated one (calibrate()), else TAU
        self.tau = TAU if tau is None else float(tau)
        self._tau_given = tau is not None
        self.decay = decay
        self.author = author
        self._ids: list[str] = []
        self._outcomes: list[str | None] = []
        self._authors: list[str] = []
        self._ts: list[float] = []
        self._situations: list[Any] = []
        self._meta: dict[str, Any] = {}
        self._trajs: list[np.ndarray | None] = []
        self._glyphs: list[np.ndarray] = []
        self._matrix = np.empty((0, dim), dtype=np.float32)  # grown 2x
        self._n = 0
        self._db = None
        # the exact weighted score, fast: per leaf path, every distinct value's glyph once (a table and a
        # matrix), and per precedent the index of its value there (-1: the precedent has no such leaf)
        self._leaf_index: dict[str, dict[str, int]] = {}
        self._leaf_vectors: dict[str, list[np.ndarray]] = {}
        self._leaf_matrix: dict[str, np.ndarray | None] = {}
        self._leaf_of: dict[str, list[int]] = {}
        if path:
            self._open(path)

    # leaves: the fast path's tables

    @staticmethod
    def _leaves(situation: Any, at: str = "") -> list[tuple[str, Any]]:
        """Every leaf of a situation as (path, value): an object's keys nest, anything else is a leaf."""
        if isinstance(situation, dict):
            out: list[tuple[str, Any]] = []
            for key in sorted(situation):
                out += Space._leaves(situation[key], f"{at}/{key}")
            return out
        return [(at, situation)]

    def _leaf_add(self, situation: Any) -> None:
        """One more precedent's leaves, in every table (a leaf it lacks: -1)."""
        n = self._n  # its row, once appended
        present: dict[str, Any] = dict(self._leaves(situation))
        for path in set(self._leaf_of) | set(present):
            rows = self._leaf_of.setdefault(path, [])
            rows += [-1] * (n - len(rows))
            if path in present:
                key = json.dumps(present[path], sort_keys=True, ensure_ascii=False)
                index = self._leaf_index.setdefault(path, {})
                if key not in index:
                    index[key] = len(index)
                    self._leaf_vectors.setdefault(path, []).append(
                        _encode(present[path], None, self.seed, self.dim).astype(np.float32)
                    )
                    self._leaf_matrix[path] = None  # rebuilt on the next read
                rows.append(index[key])
            else:
                rows.append(-1)

    def _leaf_drop(self, i: int) -> None:
        for rows in self._leaf_of.values():
            if i < len(rows):
                del rows[i]

    def _leaf_sims(self, path: str, query_vector: np.ndarray) -> np.ndarray | None:
        """Every precedent's leaf sim with the query's leaf at this path; None when no precedent has the leaf."""
        vectors = self._leaf_vectors.get(path)
        if not vectors:
            return None
        matrix = self._leaf_matrix.get(path)
        if matrix is None or len(matrix) != len(vectors):
            matrix = np.stack(vectors)
            self._leaf_matrix[path] = matrix
        by_value = (matrix @ query_vector.astype(np.float32)) / self.dim
        rows = self._leaf_of[path]
        rows = rows + [-1] * (self._n - len(rows))
        index = np.array(rows[: self._n], dtype=np.int64)
        sims = np.where(index >= 0, by_value[np.maximum(index, 0)], 0.0)
        return sims.astype(np.float64)

    def _has(self, path: str) -> np.ndarray:
        rows = self._leaf_of.get(path)
        if rows is None:
            return np.zeros(self._n, dtype=bool)
        return np.array((rows + [-1] * (self._n - len(rows)))[: self._n], dtype=np.int64) >= 0

    def _present(self, prefix: str, weights: dict[str, Any] | None) -> np.ndarray:
        """Which precedents weigh something at this path: a leaf there, or a leaf with weight under it."""
        found = self._has(prefix)
        for k in self._keys_under(prefix):
            own, inner = _weight(weights.get(k) if weights else None)
            if own > 0:
                found |= self._present(f"{prefix}/{k}", inner)
        return found

    def _has_any(self, prefix: str) -> np.ndarray:
        """Which precedents carry anything at this path or under it, weighed or not."""
        found = self._has(prefix)
        for path in self._leaf_of:
            if path.startswith(prefix + "/"):
                found |= self._has(path)
        return found

    def _keys_under(self, prefix: str) -> set[str]:
        """The keys the precedents have directly under this path."""
        cut = len(prefix) + 1
        return {path[cut:].split("/", 1)[0] for path in self._leaf_of if path.startswith(prefix + "/")}

    def _fast_scores(self, situation: dict[str, Any], weights: dict[str, Any] | None) -> np.ndarray:
        """The exact weighted sim of the situation against every precedent, as _exact scores it, by leaf and in
        one pass: a node's score is the weighted mean of its children's (a key on one side only scores 0 at its
        weight), a leaf's the glyph sim of the two values. The query's side fixes the weights; the only thing that
        varies by precedent is which keys it has, so each node is a numerator and a denominator over n precedents,
        and each leaf one matrix product."""
        n = self._n

        def node(q: Any, w: dict[str, Any] | None, at: str) -> np.ndarray:
            if not isinstance(q, dict):
                sims = self._leaf_sims(at, _encode(q, None, self.seed, self.dim))
                return np.zeros(n) if sims is None else sims
            owns = {k: _weight(w.get(k) if w else None) for k in q}
            numerator = np.zeros(n)
            denominator = np.zeros(n)
            for k, (own, inner) in owns.items():
                if own > 0 and not _weighs_nothing(q[k], inner):
                    # the key counts for a precedent that weighs something there (present, or missing something
                    # weighed); for one that weighs nothing there it is out of the mean, as _explain has it
                    there = self._present(f"{at}/{k}", inner)
                    denominator += own * (there | ~self._has_any(f"{at}/{k}"))
                    numerator += own * node(q[k], inner, f"{at}/{k}")
            for k in self._keys_under(at) - set(q):
                own, inner = _weight(w.get(k) if w else None)
                if own > 0:
                    denominator += own * self._present(f"{at}/{k}", inner)
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.where(denominator > 0, numerator / denominator, 0.0)

        return node(situation, weights, "")

    # encoding: a situation is an object (values may nest — see
    # encode()); optional events fold in as a strand so order-carrying
    # situations keep their order; weights weigh the situation's keys
    def encode(
        self, situation: dict[str, Any], events: list[np.ndarray] | None = None, weights: dict[str, Any] | None = None
    ) -> np.ndarray:
        if not isinstance(situation, dict):
            raise TypeError("a situation is an object of attributes")
        parts, ws = _keyed(situation, weights, self.seed, self.dim)
        if events:
            parts.append(bind(symbol("trajectory", self.seed, self.dim), Strand(list(events)).state(self.decay)))
            ws.append(1.0)
        if not parts:
            raise ValueError("cannot encode an empty situation")
        return bundle(parts, ws)

    def _traj(self, events: list[np.ndarray] | None) -> np.ndarray | None:
        return Strand(list(events)).state(self.decay) if events else None

    def _append(
        self,
        rid: str,
        outcome: str | None,
        glyph: np.ndarray,
        author: str,
        ts: float,
        situation: Any,
        traj: np.ndarray | None,
    ) -> None:
        self._ids.append(rid)
        self._situations.append(situation)
        self._trajs.append(traj)
        self._outcomes.append(outcome or None)  # stored as "" in the file
        self._authors.append(author)
        self._ts.append(float(ts))
        self._glyphs.append(glyph)
        if self._n == len(self._matrix):  # amortized O(1) growth
            grown = np.empty((max(64, 2 * len(self._matrix)), self.dim), dtype=np.float32)
            grown[: self._n] = self._matrix[: self._n]
            self._matrix = grown
        self._matrix[self._n] = glyph.astype(np.float32)
        self._leaf_add(situation)
        self._n += 1

    def _drop(self, i: int) -> None:
        for column in (self._ids, self._situations, self._trajs, self._outcomes, self._authors, self._ts, self._glyphs):
            del column[i]
        self._leaf_drop(i)
        self._matrix[i : self._n - 1] = self._matrix[i + 1 : self._n]
        self._n -= 1

    def record(
        self,
        situation: dict[str, Any],
        outcome: str | None = None,
        events: list[np.ndarray] | None = None,
        rid: str | None = None,
        ts: float | None = None,
    ) -> str:
        rid = rid or str(uuid.uuid4())[:12]
        ts = time.time() if ts is None else float(ts)
        glyph = self.encode(situation, events)
        if rid in self._ids:  # the same id again replaces the row
            self._drop(self._ids.index(rid))
        self._append(rid, outcome, glyph, self.author, ts, situation, self._traj(events))
        if self._db is not None:
            blob = np.stack(events).astype(np.int8).tobytes() if events else None
            self._db.execute(
                "INSERT OR REPLACE INTO precedents (id, situation, outcome,"
                " dim, glyph, author, ts, events)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    json.dumps(situation, ensure_ascii=False),
                    outcome or "",
                    self.dim,
                    glyph.tobytes(),
                    self.author,
                    ts,
                    blob,
                ),
            )
            self._db.commit()
        return rid

    def _sims(
        self, situation: dict[str, Any], events: list[np.ndarray] | None, weights: dict[str, Any] | None = None
    ) -> np.ndarray:
        q = self.encode(situation, events, weights).astype(np.float32)
        return (self._matrix[: self._n] @ q) / self.dim

    def _exact(
        self,
        i: int,
        situation: dict[str, Any],
        traj: np.ndarray | None,
        weights: dict[str, Any] | None,
        glyphs: bool = False,
    ) -> Fact:
        """Precedent i explained against the situation; events, when
        either side has them, are one more key: "~events"."""
        root = _explain("", 1.0, situation, self._situations[i], weights, self.seed, self.dim, glyphs)
        theirs = self._trajs[i]
        if (traj is not None or theirs is not None) and root.children:
            own, _ = _weight(weights.get("~events") if weights else None)
            if traj is not None and theirs is not None:
                s = sim(traj, theirs)
                fact = Fact("~events", own, s, 0.0, s, "both", "…", "…", [])
            else:
                fact = Fact(
                    "~events",
                    own,
                    0.0,
                    0.0,
                    0.0,
                    "query_only" if theirs is None else "record_only",
                    "…" if traj is not None else _ABSENT,
                    "…" if theirs is not None else _ABSENT,
                    [],
                )
            root.children.append(fact)
            root.score = _tally(root.children)
        root.share = root.score
        return root

    def _scores(
        self, situation: dict[str, Any], events: list[np.ndarray] | None, weights: dict[str, Any] | None
    ) -> np.ndarray:
        """The similarity every read ranks and gates on: the glyph sim,
        or — when the caller weighs the keys — the exact weighted sim,
        so a weight can raise a match as well as lower one."""
        if weights is None:
            return self._sims(situation, events)
        self.encode(situation, events, weights)  # same errors either way
        traj = self._traj(events)
        if traj is None and not any(t is not None for t in self._trajs[: self._n]):
            return self._fast_scores(situation, weights)
        return np.array([self._exact(i, situation, traj, weights).score for i in range(self._n)], dtype=np.float64)

    def _top(
        self,
        situation: dict[str, Any],
        events: list[np.ndarray] | None,
        weights: dict[str, Any] | None = None,
        k: int | None = None,
    ) -> list[tuple[int, float]]:
        sims = self._scores(situation, events, weights)
        # highest first; equal scores, the later row first (stable, so
        # the order holds at any size and in every language)
        order = np.argsort(sims, kind="stable")[::-1][: self.k if k is None else k]
        return [(int(i), float(sims[i])) for i in order]

    def _hash(self, i: int) -> str:
        return hashlib.sha256(self._glyphs[i].tobytes()).hexdigest()[:16]

    def query(
        self, situation: dict[str, Any], events: list[np.ndarray] | None = None, weights: dict[str, Any] | None = None
    ) -> Answer:
        """weights: the query's weights tree (see encode()). Weighted,
        every similarity here is the exact weighted sim (a fact tree's
        score), so min_sim reads as 'the weighted share of attributes
        that must agree'. Unweighted, it is the glyph sim, as always."""
        if self._n == 0:
            return Answer(False, 0.0, 0.0, 0, None, {}, [])
        top = self._top(situation, events, weights)
        receipts = [(self._ids[i], s, self._outcomes[i], self._hash(i), self._authors[i]) for i, s in top]
        # only precedents that clear the similarity bar get a vote
        voters = [(i, s) for i, s in top if s >= self.min_sim and self._outcomes[i] is not None]
        if not voters:
            return Answer(False, 0.0, max(top[0][1], 0.0), 0, None, {}, receipts)
        weights = np.array([s for _, s in voters])
        wsum = float(weights.sum()) or 1.0
        outcomes: dict[str, float] = {}
        for (i, _), w in zip(voters, weights):
            outcomes[self._outcomes[i]] = outcomes.get(self._outcomes[i], 0.0) + float(w)
        outcomes = {o: w / wsum for o, w in outcomes.items()}
        top_outcome = max(outcomes, key=outcomes.get)
        return Answer(True, outcomes[top_outcome], float(weights.mean()), len(voters), top_outcome, outcomes, receipts)

    def _evidence(self, sims: np.ndarray, outcome: str, bar: float) -> Evidence:
        idx = [i for i in range(self._n) if self._outcomes[i] == outcome and sims[i] >= bar]
        if not idx:
            return Evidence(outcome, 0, 0.0, 0.0, 0.0, [])
        close = sims[idx]
        return Evidence(
            outcome,
            len(idx),
            float(close.mean()),
            float(close.max()),
            max(self._ts[i] for i in idx),
            sorted({self._authors[i] for i in idx}),
        )

    def evidence(
        self,
        situation: dict[str, Any],
        outcome: str,
        events: list[np.ndarray] | None = None,
        min_sim: float | None = None,
        weights: dict[str, Any] | None = None,
    ) -> Evidence:
        """Support for ONE candidate outcome, over every precedent (not
        just the top k), judged on its own: how many precedents with
        this outcome clear the bar, how close, how recent, by whom."""
        if self._n == 0:
            return Evidence(outcome, 0, 0.0, 0.0, 0.0, [])
        bar = self.min_sim if min_sim is None else min_sim
        return self._evidence(self._scores(situation, events, weights), outcome, bar)

    def answers(
        self,
        situation: dict[str, Any],
        events: list[np.ndarray] | None = None,
        min_count: int = 1,
        weights: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        """Every outcome with qualifying precedent in this situation,
        strongest first. The read for situations that genuinely have
        several true outcomes: gate each on its own count, not on its
        share of a vote (see SPEC rule 4)."""
        if self._n == 0:
            return []
        sims = self._scores(situation, events, weights)
        found = {self._outcomes[i] for i in range(self._n) if sims[i] >= self.min_sim and self._outcomes[i] is not None}
        out = [self._evidence(sims, o, self.min_sim) for o in found]
        out = [e for e in out if e.count >= min_count]
        return sorted(out, key=lambda e: (-e.count, -e.support, e.outcome))

    def matches(
        self,
        situation: dict[str, Any],
        outcome: str,
        events: list[np.ndarray] | None = None,
        weights: dict[str, Any] | None = None,
    ) -> float:
        """Max similarity among precedents with THIS outcome (0.0 if
        none). The veto primitive: 'how close is the nearest recorded
        failure of this exact action?' — a direct check that no
        consensus vote can dilute."""
        if self._n == 0:
            return 0.0
        mask = np.array([o == outcome for o in self._outcomes[: self._n]])
        if not mask.any():
            return 0.0
        sims = self._scores(situation, events, weights)[mask]
        return float(sims.max())

    def facts(
        self,
        situation: dict[str, Any],
        events: list[np.ndarray] | None = None,
        weights: dict[str, Any] | None = None,
        top: int | None = None,
        min_score: float | None = None,
    ) -> list[FactTree]:
        """The exact read: every precedent scored by weighted sim
        (unweighted keys weigh 1), the `top` (default k) best each
        explained as a fact tree — per key its score, weight and share,
        events as "~events" — with the glyph sim beside it and a
        citation. Ranked by score, then glyph sim, newest, id; rows under
        min_score (if given) are left out."""
        if self._n == 0:
            return []
        sims = self._sims(situation, events)
        scores = self._scores(situation, events, weights or {})
        order = sorted(
            (i for i in range(self._n) if min_score is None or scores[i] >= min_score),
            key=lambda i: (-scores[i], -sims[i], -self._ts[i], self._ids[i]),
        )
        traj = self._traj(events)
        probs = self._probabilities(scores, self.tau)
        return [
            FactTree(
                self._ids[i],
                self._outcomes[i],
                self._authors[i],
                self._ts[i],
                self._hash(i),
                float(sims[i]),
                self._exact(i, situation, traj, weights, glyphs=True),
                probs.get(self._outcomes[i], 0.0) if self._outcomes[i] is not None else 0.0,
            )
            for i in order[: self.k if top is None else top]
        ]

    def probabilities(
        self, situation: dict[str, Any], events: list[np.ndarray] | None = None, weights: dict[str, Any] | None = None
    ) -> dict[str, float]:
        """P(outcome | situation) for every outcome among the POOL
        nearest precedents by exact weighted sim: a softmax of their
        scores at temperature tau, summed per outcome. Calibrated once
        calibrate() has fit tau to this store's own graded record."""
        if self._n == 0:
            return {}
        return self._probabilities(self._scores(situation, events, weights or {}), self.tau)

    def _probabilities(self, scores: np.ndarray, tau: float, skip: int = -1) -> dict[str, float]:
        order = [
            int(i) for i in np.argsort(scores, kind="stable")[::-1] if i != skip and self._outcomes[i] is not None
        ][:POOL]
        if not order:
            return {}
        top = float(scores[order[0]])
        mass: dict[str, float] = {}
        for i in order:
            o = self._outcomes[i]
            mass[o] = mass.get(o, 0.0) + math.exp((float(scores[i]) - top) / tau)
        z = sum(mass.values())
        return {o: m / z for o, m in mass.items()}

    def calibrate(
        self,
        sample: int = 100,
        weights: dict[str, Any] | None = None,
        weights_for: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        cost: Callable[[dict[str, float], str], float] | None = None,
    ) -> float:
        """Fit tau so probabilities mean what they say: hold out each of
        the `sample` newest precedents in turn, score it against the
        rest, and keep the temperature (from TAUS) with the least
        log-loss on the held-out outcomes. Saved in the file, so every
        later open uses it. Events are left out. Returns tau.
        `weights_for` gives each held-out situation its own weights
        (a spec's, which depend on what the situation carries).
        `cost(probabilities, true)` is what deciding from those
        probabilities would cost when `true` is the outcome: given, the
        temperature with the least total cost wins, log-loss breaking
        ties, so the fit serves the decision rather than the curve."""
        graded = [i for i in range(self._n) if self._outcomes[i] is not None]
        if len(graded) < 10:
            raise ValueError("calibrate needs at least 10 precedents with an outcome")
        held = graded[-sample:]
        scored = [
            (h, self._scores(self._situations[h], None, weights_for(self._situations[h]) if weights_for else (weights or {})))
            for h in held
        ]
        best: tuple[float, float, float] = (float("inf"), float("inf"), TAU)
        for tau in TAUS:
            loss = paid = 0.0
            for h, scores in scored:
                p = self._probabilities(scores, tau, skip=h)
                loss -= math.log(max(p.get(self._outcomes[h], 0.0), 1e-6))
                if cost is not None:
                    paid += cost(p, self._outcomes[h])
            key = (round(paid, 9), loss) if cost is not None else (loss, 0.0)
            if key < best[:2]:
                best = (*key, tau)
        self.tau, self._tau_given = best[2], False
        if self._db is not None:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('tau', ?)", (repr(self.tau),))
            self._db.commit()
        return self.tau

    def __len__(self) -> int:
        return self._n

    def rewrite(self, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
        """Every precedent's situation passed through fn and stored again, glyph and leaf tables rebuilt (its
        events kept). For a spec that has changed what a situation carries. Returns how many were rewritten."""
        blobs: dict[str, bytes | None] = {}
        if self._db is not None:
            blobs = dict(self._db.execute("SELECT id, events FROM precedents").fetchall())
        changed = 0
        for i in range(self._n):
            new = fn(self._situations[i])
            if new == self._situations[i]:
                continue
            events = _events_from_blob(blobs.get(self._ids[i]), self.dim) if self._db is not None else None
            parts, ws = _keyed(new, None, self.seed, self.dim)
            traj = self._trajs[i]
            if traj is not None:
                parts.append(bind(symbol("trajectory", self.seed, self.dim), traj if events is None else Strand(events).state(self.decay)))
                ws.append(1.0)
            glyph = bundle(parts, ws)
            self._situations[i], self._glyphs[i] = new, glyph
            self._matrix[i] = glyph.astype(np.float32)
            if self._db is not None:
                self._db.execute(
                    "UPDATE precedents SET situation = ?, glyph = ? WHERE id = ?",
                    (json.dumps(new, ensure_ascii=False), glyph.tobytes(), self._ids[i]),
                )
            changed += 1
        if changed:
            self._leaf_index, self._leaf_vectors, self._leaf_matrix, self._leaf_of = {}, {}, {}, {}
            n, self._n = self._n, 0
            for i in range(n):
                self._leaf_add(self._situations[i])
                self._n += 1
            if self._db is not None:
                self._db.commit()
        return changed

    def graded(self) -> list[tuple[dict[str, Any], str]]:
        """Every precedent with an outcome: (situation, outcome), oldest first."""
        return [(self._situations[i], self._outcomes[i]) for i in range(self._n) if self._outcomes[i] is not None]

    def meta(self, key: str) -> Any:
        """A value kept with the store (JSON), or None: what a model learned from it, for one."""
        if self._db is None:
            return self._meta.get(key)
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        if self._db is None:
            self._meta[key] = value
            return
        self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value, sort_keys=True)))
        self._db.commit()

    # persistence: one portable file; WAL so several agents can share it
    def _open(self, path: str) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(p), timeout=10.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        _ensure_schema(self._db)
        if _file_encoding(self._db) < ENCODING:
            _reencode_rows(self._db, self)
        self._db.commit()
        saved = self._db.execute("SELECT value FROM meta WHERE key = 'tau'").fetchone()
        if saved and not self._tau_given:
            self.tau = float(saved[0])
        self.refresh()

    def refresh(self) -> int:
        """Pull in rows other agents wrote to this file since open.
        Returns how many new precedents arrived."""
        if self._db is None:
            return 0
        known = set(self._ids)
        added = 0
        for rid, situation, outcome, dim, blob, author, ts, ev in self._db.execute(
            "SELECT id, situation, outcome, dim, glyph, author, ts, events FROM precedents ORDER BY ts, rowid"
        ):
            if rid in known:
                continue
            if dim != self.dim:
                raise ValueError(f"precedent {rid} has dim {dim}, space is {self.dim}")
            self._append(
                rid,
                outcome,
                np.frombuffer(blob, dtype=np.int8).copy(),
                author,
                ts,
                _situation_of(situation),
                self._traj(_events_from_blob(ev, dim)),
            )
            added += 1
        return added

    def merge(self, other_path: str) -> int:
        """Union another Space file into this one (by precedent id).
        The exchange primitive between agents: ship the file, verify
        its receipts against your own world, then merge. Rows from a
        file written under an older encoding are re-encoded from their
        situations on the way in. Returns how many precedents were
        new."""
        other = sqlite3.connect(str(Path(other_path).expanduser()))
        cols = [r[1] for r in other.execute("PRAGMA table_info(precedents)")]
        stale = _file_encoding(other) < ENCODING
        select = "SELECT id, situation, outcome, dim, glyph, {a}, {t}, {e} FROM precedents".format(
            a="author" if "author" in cols else "'anon'",
            t="ts" if "ts" in cols else "0.0",
            e="events" if "events" in cols else "NULL",
        )
        known = set(self._ids)
        added = 0
        for rid, situation, outcome, dim, blob, author, ts, ev in other.execute(select):
            if rid in known:
                continue
            if dim != self.dim:
                raise ValueError(f"precedent {rid} has dim {dim}, space is {self.dim}")
            if stale:
                glyph = self.encode(json.loads(situation), _events_from_blob(ev, dim))
            else:
                glyph = np.frombuffer(blob, dtype=np.int8).copy()
            self._append(
                rid, outcome, glyph, author, ts, _situation_of(situation), self._traj(_events_from_blob(ev, dim))
            )
            if self._db is not None:
                self._db.execute(
                    "INSERT OR REPLACE INTO precedents (id, situation,"
                    " outcome, dim, glyph, author, ts, events)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, situation, outcome, dim, glyph.tobytes(), author, ts, ev),
                )
            added += 1
        if self._db is not None:
            self._db.commit()
        other.close()
        return added

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> Space:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── the contract as a function ──────────────────────────────────────


@dataclass
class Decision:
    """What decide() concluded. Act on `outcome` only when `act`.

    reason: 'reflex' (act), 'insufficient' (nothing clears min_sim),
    'uncalibrated' (fewer than min_count precedents), 'low_confidence'
    (no consensus), or 'vetoed' (a recorded failure is as close as the
    wins and newer than them). `lifted` is True when a close veto
    exists but a newer win outranks it."""

    act: bool
    outcome: str | None
    reason: str
    answer: Answer
    veto: Evidence | None
    lifted: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "act": self.act,
            "outcome": self.outcome,
            "reason": self.reason,
            "lifted": self.lifted,
            "veto": self.veto.to_json() if self.veto else None,
            "answer": self.answer.to_json(),
        }


def decide(
    wins: Space,
    vetoes: Space,
    situation: dict[str, Any],
    events: list[np.ndarray] | None = None,
    *,
    min_confidence: float = 0.5,
    min_count: int = 3,
    min_veto_count: int = 1,
    weights: dict[str, Any] | None = None,
) -> Decision:
    """THE CONTRACT (see SPEC): the three trust gates on the wins, then
    the veto check, with the newest evidence winning.

    A veto of the top outcome counts when a recorded failure is at
    least as close as the wins recommending it (sim >= answer.support).
    It blocks while at least `min_veto_count` such failures exist AND
    the newest of them is newer than the newest qualifying win; a later
    reproduction lifts it. min_veto_count=1 is fail-closed (one failure
    stops the reflex); 2+ demands corroboration (harder to sabotage).
    weights weigh the situation's keys in every read (see encode())."""
    answer = wins.query(situation, events, weights)
    if not answer.sufficient:
        return Decision(False, None, "insufficient", answer, None, False)
    top = answer.top
    veto = vetoes.evidence(situation, top, events, min_sim=max(answer.support, 1e-9), weights=weights)
    win = wins.evidence(situation, top, events, weights=weights)
    close = veto.count >= min_veto_count
    lifted = close and win.latest > veto.latest
    if close and not lifted:
        return Decision(False, top, "vetoed", answer, veto, False)
    if answer.count < min_count:
        return Decision(False, top, "uncalibrated", answer, veto if veto.count else None, lifted)
    if answer.confidence < min_confidence:
        return Decision(False, top, "low_confidence", answer, veto if veto.count else None, lifted)
    return Decision(True, top, "reflex", answer, veto if veto.count else None, lifted)


# ── store schema and migration ──────────────────────────────────────


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS precedents ("
        " id TEXT PRIMARY KEY, situation TEXT NOT NULL,"
        " outcome TEXT NOT NULL, dim INTEGER NOT NULL,"
        " glyph BLOB NOT NULL, author TEXT NOT NULL DEFAULT 'anon',"
        " ts REAL NOT NULL DEFAULT 0, events BLOB)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS meta ( key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    cols = [r[1] for r in db.execute("PRAGMA table_info(precedents)")]
    # migrate older files in place
    if "author" not in cols:
        db.execute("ALTER TABLE precedents ADD COLUMN author TEXT NOT NULL DEFAULT 'anon'")
    if "ts" not in cols:
        db.execute("ALTER TABLE precedents ADD COLUMN ts REAL NOT NULL DEFAULT 0")
    if "events" not in cols:
        db.execute("ALTER TABLE precedents ADD COLUMN events BLOB")
    rows = db.execute("SELECT COUNT(*) FROM precedents").fetchone()[0]
    stamped = db.execute("SELECT value FROM meta WHERE key = 'encoding'").fetchone()
    if stamped is None:
        # an empty file starts on the current encoding; a populated
        # file without a stamp predates stamping (encoding 1)
        db.execute("INSERT INTO meta VALUES ('encoding', ?)", (str(ENCODING if rows == 0 else 1),))


def _file_encoding(db: sqlite3.Connection) -> int:
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'encoding'").fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        return int(row[0])
    try:
        rows = db.execute("SELECT COUNT(*) FROM precedents").fetchone()[0]
    except sqlite3.Error:
        rows = 0
    return ENCODING if rows == 0 else 1


def _situation_of(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except (TypeError, json.JSONDecodeError):
        return None


def _events_from_blob(blob: bytes | None, dim: int) -> list[np.ndarray] | None:
    if not blob:
        return None
    flat = np.frombuffer(blob, dtype=np.int8)
    return [row.copy() for row in flat.reshape(-1, dim)]


def _reencode_rows(db: sqlite3.Connection, space: Space) -> int:
    rows = db.execute("SELECT id, situation, dim, events FROM precedents").fetchall()
    done = 0
    for rid, situation_json, dim, ev in rows:
        try:
            glyph = space.encode(json.loads(situation_json), _events_from_blob(ev, space.dim))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue  # no usable situation stored: keep the old glyph
        db.execute("UPDATE precedents SET glyph = ?, dim = ? WHERE id = ?", (glyph.tobytes(), space.dim, rid))
        done += 1
    db.execute("INSERT OR REPLACE INTO meta VALUES ('encoding', ?)", (str(ENCODING),))
    return done
