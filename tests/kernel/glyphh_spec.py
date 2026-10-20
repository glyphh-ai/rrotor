"""
The Glyphh spec — a model's typed config, and how data in that shape
meets the kernel. numpy + stdlib, beside glyphh_kernel.py.

A spec is JSON: a cortex of layers, each layer a cortex of segments,
each segment a cortex of roles. Every level carries a similarity_weight
(its voice in the bundle above it) and a security_weight (the clearance
a reader needs to see it).

    {"name": "tickets", "dimension": 4096, "seed": 42,
     "temporal_source": "meta.time.opened",
     "layers": [
       {"name": "issue", "similarity_weight": 1.0, "segments": [
         {"name": "body", "roles": [
           {"name": "title", "text_encoding": "bag_of_words"},
           {"name": "component", "lexicons": ["area"]},
           {"name": "severity", "numeric_config":
              {"min_value": 0, "max_value": 10, "bin_width": 1}},
           {"name": "id", "key_part": true, "similarity_weight": 0}]}]},
       {"name": "meta", "security_weight": 0.8, "segments": [
         {"name": "time", "roles": [{"name": "opened"}]}]}],
     "output": {"report": ["issue"]}}

Data in that model is the same shape, by name:
    {"issue": {"body": {"title": "pods crash on boot", "area": "kubelet",
                        "severity": 7, "id": "K-1"}},
     "meta": {"time": {"opened": "2026-10-01T12:00:00Z"}}}

How it meets the kernel. The data nests layer > segment > role, and the
kernel's encode() bundles each object weighted by its keys: the role
bindings bundle into the segment's cortex, segments into the layer's,
layers into the model's. A role's value is a symbol by default (an array
is a set of symbols); text_encoding "bag_of_words" makes it the set of its
words; numeric_config makes it a thermometer, the set of every bin edge
at or below the value, so near numbers share most of their bins.
lexicons are other names the role answers to in data. key_part roles
make the record's id (a later record with the same key replaces it).
temporal_source names the role that stamps the record's time; it is a
timestamp beside the glyph, never a part of it.

Reads. A query scores only the roles it gives: a role the query leaves
out weighs 0. clearance (0..1) is the reader's: every layer, segment or
role whose security_weight is above it weighs 0 in the score and is cut
from the fact tree. output.report keeps only the named paths in the
fact trees that come back ("layer", "layer.segment" or
"layer.segment.role"); empty means everything the reader may see.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import glyphh_kernel as kernel
from glyphh_kernel import ReadOnlyError  # noqa: F401  (a read-only model's writes raise it)

SPEC_VERSION = 1
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$")
_WORD = re.compile(r"[a-z0-9']+")


class SpecError(ValueError):
    """A spec, or data offered to one, that does not match it. `path`
    names the offending field."""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


# ── the spec ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NumericConfig:
    min_value: float
    max_value: float
    bin_width: float

    def bins(self, x: float) -> list[str]:
        """Thermometer: every bin edge at or below x, clamped to range."""
        x = min(max(x, self.min_value), self.max_value)
        top = int((x - self.min_value) // self.bin_width)
        return [kernel._text(self.min_value + i * self.bin_width) for i in range(top + 1)]


@dataclass(frozen=True)
class Role:
    name: str
    similarity_weight: float = 1.0
    security_weight: float = 0.0
    key_part: bool = False
    numeric_config: NumericConfig | None = None
    text_encoding: str | None = None
    lexicons: tuple[str, ...] = ()
    # how much each value of this role weighs, 0 to 1, on top of similarity_weight: learned from the store
    # (learn()), keyed by the value's text, for a number by its bin's lower edge. A value not named weighs 1.
    value_weights: tuple[tuple[str, float], ...] = ()

    def value_weight(self, value: Any) -> float:
        if not self.value_weights or value is None:
            return 1.0
        key = kernel._text(value[-1]) if self.numeric_config is not None and isinstance(value, list) else None
        if key is None:
            if isinstance(value, list):
                return 1.0  # a set or a bag of words: the role's weight alone
            key = kernel._text(value)
        return dict(self.value_weights).get(key, 1.0)


@dataclass(frozen=True)
class Segment:
    name: str
    roles: tuple[Role, ...]
    similarity_weight: float = 1.0
    security_weight: float = 0.0


@dataclass(frozen=True)
class Layer:
    name: str
    segments: tuple[Segment, ...]
    similarity_weight: float = 1.0
    security_weight: float = 0.0


@dataclass(frozen=True)
class Interaction:
    """Two roles read together: a case's value is the pair of theirs, in a layer the spec adds itself
    (`interactions.pairs.<name>`). What one role means can depend on another (a bad checking account with a long
    loan); learn() finds the pairs that tell more together than apart. Secured as the stricter of its two roles."""

    name: str
    roles: tuple[str, str]
    similarity_weight: float = 1.0
    value_weights: tuple[tuple[str, float], ...] = ()

    def value_weight(self, value: Any) -> float:
        if not self.value_weights or value is None:
            return 1.0
        return dict(self.value_weights).get(kernel._text(value), 1.0)


@dataclass(frozen=True)
class Settings:
    """The model's levers, in the spec (`settings`) and changeable live (Model.configure): how a query votes, how
    learning weighs words and pairs, how calibration fits, and whether the model refreshes itself. Each is checked
    by name and range; a change to a learning lever relearns."""

    k: int = 15  # nearest precedents a query votes over
    min_sim: float = 0.10  # the weighted similarity a precedent needs to vote
    word_weights: bool = True  # learn a weight per word of a text role
    word_floor: float = 0.35  # the least a learned word weighs (a bag's glyph is a majority vote: keep the quiet words heard)
    interactions: bool = True  # learn pairs of roles that tell more together
    max_interactions: int = 5  # at most this many pairs
    rare: int = 5  # a value, word or pair seen fewer times than this is not learned from
    calibrate_by: str = "loss"  # "loss" (probabilities that mean what they say) or "cost" (that decide best)
    calibrate_sample: int = 200  # the newest precedents a calibration holds out
    refresh_every: int | None = None  # learn and calibrate again every so many graded records; None: on demand
    lexicon: bool = True  # read a text role's words through the kernel's seeds (synonyms, near words, classes)
    word_pairs: bool = False  # learn pairs of words that tell more together than apart (off: on Banking77 it took
    # 0.1 to 0.4 points inside the training set; a lever for a model whose intents are phrases)
    max_word_pairs: int = 20  # at most this many pairs per text role
    word_evidence: float = 2.0  # how much a query's words, by what each says of every outcome, weigh on the vote
    # (0: not at all; 2 chosen on Banking77 inside its training set, 83.4% to 88.0%)

    RANGES = {  # name: (kind, low, high) or (kind, choices)
        "k": (int, 1, 1000),
        "min_sim": (float, 0.0, 1.0),
        "word_weights": (bool,),
        "word_floor": (float, 0.0, 1.0),
        "interactions": (bool,),
        "max_interactions": (int, 0, 50),
        "rare": (int, 1, 100000),
        "calibrate_by": (str, ("loss", "cost")),
        "calibrate_sample": (int, 10, 1000000),
        "refresh_every": (int, 10, 1000000),
        "lexicon": (bool,),
        "word_pairs": (bool,),
        "max_word_pairs": (int, 0, 200),
        "word_evidence": (float, 0.0, 5.0),
    }
    LEARNING = ("word_weights", "word_floor", "interactions", "max_interactions", "rare", "word_pairs", "max_word_pairs")

    def with_changes(self, changes: Mapping[str, Any], at: str = "settings") -> Settings:
        """These settings with the changes put in, each checked; a bad name, kind or value raises SpecError."""
        values = {k: getattr(self, k) for k in self.RANGES}
        for name, value in changes.items():
            if name not in self.RANGES:
                raise SpecError(f"{at}.{name}", f"no such setting; the settings are {', '.join(self.RANGES)}")
            kind = self.RANGES[name][0]
            if name == "refresh_every" and value is None:
                values[name] = None
                continue
            if kind is bool:
                if not isinstance(value, bool):
                    raise SpecError(f"{at}.{name}", "true or false")
            elif kind is str:
                if value not in self.RANGES[name][1]:
                    raise SpecError(f"{at}.{name}", f"one of {', '.join(self.RANGES[name][1])}")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise SpecError(f"{at}.{name}", "a number")
            else:
                low, high = self.RANGES[name][1], self.RANGES[name][2]
                if kind is int and int(value) != value:
                    raise SpecError(f"{at}.{name}", "a whole number")
                if not low <= value <= high:
                    raise SpecError(f"{at}.{name}", f"from {low} to {high}")
                value = kind(value)
            values[name] = value
        return Settings(**values)

    def to_json(self, all: bool = False) -> dict[str, Any]:
        """The settings as an object; only those that differ from the defaults unless `all`."""
        defaults = Settings()
        return {k: getattr(self, k) for k in self.RANGES if all or getattr(self, k) != getattr(defaults, k)}


INTERACTIONS = "interactions"  # the layer the spec adds; "pairs" its one segment
PAIR_CANDIDATES = 120  # the most common words of a text role among which word pairs are looked for



@dataclass(frozen=True)
class Output:
    report: tuple[str, ...] = ()
    # the cost of deciding one outcome when another is true: costs[decided][true]; a pair not named costs 0
    # when they are the same outcome and 1 otherwise. decide() picks the outcome with the least expected cost.
    costs: tuple[tuple[str, tuple[tuple[str, float], ...]], ...] = ()
    # the cost of asking a person instead of deciding; set, decide() asks whenever every outcome's expected cost
    # is above it (the band where either answer is costly). Unset, decide() never asks.
    ask: float | None = None

    def cost(self, decided: str, true: str) -> float:
        table = dict((d, dict(t)) for d, t in self.costs)
        if decided in table and true in table[decided]:
            return table[decided][true]
        return 0.0 if decided == true else 1.0


@dataclass(frozen=True)
class Spec:
    name: str
    layers: tuple[Layer, ...]
    dimension: int = kernel.DIM
    seed: int = 42
    similarity_weight: float = 1.0
    security_weight: float = 0.0
    temporal_source: str = "auto"
    output: Output = field(default_factory=Output)
    interactions: tuple[Interaction, ...] = ()
    settings: Settings = field(default_factory=Settings)

    def with_settings(self, settings: Settings) -> Spec:
        return dataclasses.replace(self, settings=settings)

    # -- JSON in and out --------------------------------------------

    @classmethod
    def from_json(cls, obj: Any) -> Spec:
        """Validated exactly: an unknown field, a missing one or a value
        of the wrong kind raises SpecError naming where."""
        return _parse_spec(obj)

    def to_json(self) -> dict[str, Any]:
        def role(r: Role) -> dict[str, Any]:
            out: dict[str, Any] = {
                "name": r.name,
                "similarity_weight": r.similarity_weight,
                "security_weight": r.security_weight,
                "key_part": r.key_part,
            }
            if r.numeric_config:
                n = r.numeric_config
                out["numeric_config"] = {"min_value": n.min_value, "max_value": n.max_value, "bin_width": n.bin_width}
            if r.text_encoding:
                out["text_encoding"] = r.text_encoding
            if r.lexicons:
                out["lexicons"] = list(r.lexicons)
            if r.value_weights:
                out["value_weights"] = dict(r.value_weights)
            return out

        return {
            "version": SPEC_VERSION,
            "name": self.name,
            "dimension": self.dimension,
            "seed": self.seed,
            "similarity_weight": self.similarity_weight,
            "security_weight": self.security_weight,
            "temporal_source": self.temporal_source,
            "output": {
                "report": list(self.output.report),
                **({"costs": {d: dict(t) for d, t in self.output.costs}} if self.output.costs else {}),
                **({"ask": self.output.ask} if self.output.ask is not None else {}),
            },
            **({"settings": self.settings.to_json()} if self.settings.to_json() else {}),
            **(
                {
                    INTERACTIONS: [
                        {
                            "name": i.name,
                            "roles": list(i.roles),
                            "similarity_weight": i.similarity_weight,
                            **({"value_weights": dict(i.value_weights)} if i.value_weights else {}),
                        }
                        for i in self.interactions
                    ]
                }
                if self.interactions
                else {}
            ),
            "layers": [
                {
                    "name": l.name,
                    "similarity_weight": l.similarity_weight,
                    "security_weight": l.security_weight,
                    "segments": [
                        {
                            "name": s.name,
                            "similarity_weight": s.similarity_weight,
                            "security_weight": s.security_weight,
                            "roles": [role(r) for r in s.roles],
                        }
                        for s in l.segments
                    ],
                }
                for l in self.layers
            ],
        }

    def learned(self, cases: Sequence[tuple[Any, str]]) -> Spec:
        """This spec with its weights learned from graded cases: each role's similarity_weight is how much knowing
        its value tells about the outcome (information gain, scaled so the most telling role weighs 1), and each
        value's weight how far that value moves the outcome from the base rate (scaled so the most telling value
        of a role weighs 1). Key parts, text roles and roles that carry sets keep their weights (a set has no one
        value to weigh). Layers' and segments' weights are left as they are; so are security weights."""
        return self.with_weights(self.learn([(self.situation(data), outcome) for data, outcome in cases]))

    def learn(self, situations: Sequence[tuple[Mapping[str, Any], str]]) -> dict[str, dict[str, Any]]:
        """What graded situations (already in the kernel's terms) teach: per role path, its similarity_weight and
        value_weights, as learned() sets them. A model learns from its own store this way."""
        base = Counter(outcome for _, outcome in situations)
        if len(base) < 2:
            raise SpecError("", "learn needs cases with at least two outcomes")
        h_base = _entropy(base)
        gains: dict[str, float] = {}
        moves: dict[str, dict[str, float]] = {}
        words_learned: dict[str, dict[str, float]] = {}
        text_paths: list[str] = []
        base_rate = {o: n / sum(base.values()) for o, n in base.items()}
        for layer in self.layers:
            for seg in layer.segments:
                for role in seg.roles:
                    path = f"{layer.name}.{seg.name}.{role.name}"
                    if role.text_encoding:
                        text_paths.append(path)
                        if self.settings.word_weights:
                            learned_words = self._learn_words(layer.name, seg.name, role.name, situations, base_rate)
                            if learned_words:
                                words_learned[path] = learned_words
                        continue
                    if role.key_part:
                        continue
                    graded = [
                        (situation.get(layer.name, {}).get(seg.name, {}).get(role.name), outcome)
                        for situation, outcome in situations
                    ]
                    if role.numeric_config is None and any(isinstance(v, list) for v, _ in graded):
                        continue  # a role of sets: no one value to weigh; left alone, as a text role is
                    groups: dict[str, Counter] = defaultdict(Counter)
                    for value, outcome in graded:
                        if value is None:
                            continue
                        key = kernel._text(value[-1]) if role.numeric_config is not None else kernel._text(value)
                        groups[key][outcome] += 1
                    total = sum(sum(g.values()) for g in groups.values())
                    if not total:
                        continue
                    after = sum(sum(g.values()) / total * _entropy(g) for g in groups.values())
                    gains[path] = max(h_base - after, 0.0)
                    moves[path] = {
                        key: max(abs(g[o] / sum(g.values()) - base_rate.get(o, 0.0)) for o in base)
                        for key, g in groups.items()
                    }
        pairs = self._learn_pairs(situations, gains, base, base_rate, h_base) if self.settings.interactions else []
        top = max([*gains.values(), *(p["similarity_weight"] for p in pairs)], default=0.0)  # the strongest weighs 1
        learned: dict[str, Any] = {}
        for path, gain in gains.items():
            if top > 0:
                learned[path] = {"similarity_weight": round(gain / top, 4)}
                best = max(moves[path].values(), default=0.0)
                if best > 0:
                    learned[path]["value_weights"] = {k: round(m / best, 4) for k, m in moves[path].items()}
        if top > 0:
            for pair in pairs:
                pair["similarity_weight"] = round(pair["similarity_weight"] / top, 4)
            learned["~" + INTERACTIONS] = [p for p in pairs if p["similarity_weight"] > 0.1][: self.settings.max_interactions]
        for path, weights in words_learned.items():
            learned[path] = {**learned.get(path, {}), "value_weights": weights}
        evidence = {path: self._learn_evidence(path, situations) for path in text_paths}
        if evidence:
            learned["~evidence"] = evidence
        return learned

    def _learn_evidence(self, path: str, situations: Sequence[tuple[Mapping[str, Any], str]]) -> dict[str, Any]:
        """What each word says of each outcome: the counts a model turns into pointwise mutual information at query
        time (Model.bias), for words seen at least `rare` times. Not a weight in the spec: evidence the store holds."""
        lname, sname, rname = path.split(".")
        outcomes: Counter = Counter()
        words: dict[str, Counter] = defaultdict(Counter)
        total = 0
        for situation, outcome in situations:
            value = situation.get(lname, {}).get(sname, {}).get(rname)
            if value is None:
                continue
            total += 1
            outcomes[outcome] += 1
            for word in set(_words_of(value)):
                words[word][outcome] += 1
        kept = {w: dict(c) for w, c in words.items() if sum(c.values()) >= self.settings.rare}
        return {"total": total, "outcomes": dict(outcomes), "words": kept}

    def _learn_words(
        self,
        lname: str,
        sname: str,
        rname: str,
        situations: Sequence[tuple[Mapping[str, Any], str]],
        base_rate: Mapping[str, float],
    ) -> dict[str, float]:
        """What each word of a text role says about the outcome: how far the word's presence moves some outcome
        from its base rate (the same measure a field's values get; not information gain, which with many outcomes
        crowns the words that split them into big groups, "card", not the ones that name one, "stolen"), scaled
        from the settings' word_floor (tells nothing; 0.35 by default, chosen on Banking77 inside its training set:
        a bag's glyph is a majority vote, and a low floor let the few loud words drown the many quiet ones) to 1
        (the most telling word). Words seen fewer than `rare` times are left out: they get the floor."""
        seen: dict[str, Counter] = defaultdict(Counter)
        total = 0
        for situation, outcome in situations:
            value = situation.get(lname, {}).get(sname, {}).get(rname)
            if value is None:
                continue
            total += 1
            for word in _words_of(value):
                seen[word][outcome] += 1
        if total < self.settings.rare:
            return {}
        moves: dict[str, float] = {}
        for word, present in seen.items():
            n_present = sum(present.values())
            if n_present < self.settings.rare:
                continue
            moves[word] = max(max(present[o] / n_present - base_rate.get(o, 0.0) for o in present), 0.0)
        top = max(moves.values(), default=0.0)
        if top <= 0:
            return {}
        floor = self.settings.word_floor
        table = {word: round(floor + (1 - floor) * move / top, 4) for word, move in sorted(moves.items())}
        if self.settings.word_pairs and self.settings.max_word_pairs:
            common = {w: sum(seen[w].values()) for w in moves}
            table.update(self._learn_word_pairs(lname, sname, rname, situations, base_rate, moves, common, top, floor))
        return table

    def _learn_word_pairs(
        self,
        lname: str,
        sname: str,
        rname: str,
        situations: Sequence[tuple[Mapping[str, Any], str]],
        base_rate: Mapping[str, float],
        moves: Mapping[str, float],
        common: Mapping[str, int],
        top: float,
        floor: float,
    ) -> dict[str, float]:
        """Pairs of words that move the outcome further together than the better of them alone ("top" + "up",
        "card" + "arrive"), among the PAIR_CANDIDATES most common words (the rare ones are telling on their own;
        it is the common, ambiguous ones a partner settles), seen together at least `rare` times: the pair's
        extra move scaled like a word's, the max_word_pairs best kept, keyed "a+b"."""
        candidates = sorted(sorted(common, key=lambda w: -common[w])[:PAIR_CANDIDATES])
        wanted = set(candidates)
        together: dict[tuple[str, str], Counter] = defaultdict(Counter)
        for situation, outcome in situations:
            value = situation.get(lname, {}).get(sname, {}).get(rname)
            if value is None:
                continue
            present = sorted(wanted.intersection(_words_of(value)))
            for pair in itertools.combinations(present, 2):
                together[pair][outcome] += 1
        extras = {}
        for (a, b), counts in together.items():
            n = sum(counts.values())
            if n < self.settings.rare:
                continue
            move = max(counts[o] / n - base_rate.get(o, 0.0) for o in counts)
            extra = move - max(moves[a], moves[b])
            if extra > 0:
                extras[f"{a}+{b}"] = extra
        best = sorted(extras.items(), key=lambda kv: (-kv[1], kv[0]))[: self.settings.max_word_pairs]
        return {pair: round(floor + (1 - floor) * min(extra / top, 1.0), 4) for pair, extra in sorted(best)}

    def _learn_pairs(
        self,
        situations: Sequence[tuple[Mapping[str, Any], str]],
        gains: Mapping[str, float],
        base: Counter,
        base_rate: Mapping[str, float],
        h_base: float,
    ) -> list[dict[str, Any]]:
        """The pairs of roles that tell more together than the better of them alone: each pair's extra information
        gain over its best role (unscaled here; learn() scales it like a role's and keeps the settings'
        max_interactions best over a tenth of the strongest signal), values seen fewer than `rare` times pooled so
        the gain is not sparsity's."""
        paths = list(gains)
        found = []
        for a, b in itertools.combinations(paths, 2):
            groups: dict[str, Counter] = defaultdict(Counter)
            for situation, outcome in situations:
                pair = self._pair_value(situation, a, b)
                if pair is not None:
                    groups[pair][outcome] += 1
            if not groups:
                continue
            counted = Counter({k: sum(g.values()) for k, g in groups.items()})
            pooled: dict[str, Counter] = defaultdict(Counter)
            for k, g in groups.items():
                pooled[k if counted[k] >= self.settings.rare else "~"].update(g)
            total = sum(counted.values())
            after = sum(sum(g.values()) / total * _entropy(g) for g in pooled.values())
            extra = max(h_base - after, 0.0) - max(gains[a], gains[b])
            if extra > 0:
                moves = {
                    k: max(abs(g[o] / sum(g.values()) - base_rate.get(o, 0.0)) for o in base)
                    for k, g in groups.items()
                    if counted[k] >= self.settings.rare
                }
                best = max(moves.values(), default=0.0)
                found.append(
                    {
                        "name": f"{a.rsplit('.', 1)[1]}_x_{b.rsplit('.', 1)[1]}",
                        "roles": [a, b],
                        "similarity_weight": extra,
                        **({"value_weights": {k: round(m / best, 4) for k, m in moves.items()}} if best > 0 else {}),
                    }
                )
        found.sort(key=lambda f: (-f["similarity_weight"], f["name"]))
        return found

    def with_weights(self, learned: Mapping[str, Any]) -> Spec:
        """This spec with learned role weights (learn()'s table) put in, and its interactions replaced by the
        learned ones when the table names them; a path the spec lacks is ignored."""
        obj = self.to_json()
        if "~" + INTERACTIONS in learned:
            obj.pop(INTERACTIONS, None)
            if learned["~" + INTERACTIONS]:
                obj[INTERACTIONS] = [dict(i) for i in learned["~" + INTERACTIONS]]
        for layer in obj["layers"]:
            for seg in layer["segments"]:
                for role in seg["roles"]:
                    found = learned.get(f"{layer['name']}.{seg['name']}.{role['name']}")
                    if found:
                        if "similarity_weight" in found:
                            role["similarity_weight"] = found["similarity_weight"]
                        role.pop("value_weights", None)
                        if found.get("value_weights"):
                            role["value_weights"] = dict(found["value_weights"])
        return Spec.from_json(obj)

    @property
    def spec_id(self) -> str:
        """Content address: the same spec gives the same id anywhere. The settings are not part of it: they are the
        model's levers, not what a case is."""
        text = json.dumps({k: v for k, v in self.to_json().items() if k != "settings"}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    # -- data in the spec's shape -----------------------------------

    def situation(self, data: Any) -> dict[str, Any]:
        """The data, checked against the spec, in the kernel's terms:
        layer > segment > role, each value a symbol, a set of symbols, a
        bag of words or a thermometer. Empty parts are left out."""
        if not isinstance(data, dict):
            raise SpecError("", "data is an object of layers")
        out: dict[str, Any] = {}
        for lname, lval in data.items():
            layer = self._layer(lname)
            if not isinstance(lval, dict):
                raise SpecError(lname, "a layer is an object of segments")
            segs: dict[str, Any] = {}
            for sname, sval in lval.items():
                seg = self._segment(layer, sname, lname)
                if not isinstance(sval, dict):
                    raise SpecError(f"{lname}.{sname}", "a segment is an object of roles")
                roles: dict[str, Any] = {}
                for rname, rval in sval.items():
                    role = self._role(seg, rname, f"{lname}.{sname}")
                    path = f"{lname}.{sname}.{role.name}"
                    if role.name in roles:
                        raise SpecError(path, "given twice (by name and by lexicon)")
                    value = _value(role, rval, path)
                    if value is not None:
                        roles[role.name] = value
                if roles:
                    segs[seg.name] = roles
            if segs:
                out[layer.name] = segs
        if not out:
            raise SpecError("", "no role carries a value")
        return self._crossed(self._weighed(out))

    def _weighed(self, situation: dict[str, Any]) -> dict[str, Any]:
        """The situation with every text role's bag carrying the role's learned word weights ([[word, weight],
        ...]; a word not learned weighs the settings' word_floor), or plain words where the role learned none."""
        out: dict[str, Any] = {}
        for lname, segs in situation.items():
            layer = self._find(self.layers, lname)
            if layer is None:
                out[lname] = segs
                continue
            new_segs: dict[str, Any] = {}
            for sname, roles in segs.items():
                seg = self._find(layer.segments, sname)
                new_roles: dict[str, Any] = {}
                for rname, value in roles.items():
                    role = self._find(seg.roles, rname) if seg else None
                    if role is not None and role.text_encoding and isinstance(value, list):
                        words = _words_of(value)
                        table = dict(role.value_weights)
                        if table:
                            value = [[w, table.get(w, self.settings.word_floor)] for w in words]
                            have = set(words)
                            for pair, weight in table.items():
                                if "+" in pair and all(part in have for part in pair.split("+", 1)):
                                    value.append([pair, weight])
                        elif self.settings.lexicon:
                            value = [[w, 1.0] for w in words]  # a weighted set: read through the lexicon
                        else:
                            value = words
                    new_roles[rname] = value
                new_segs[sname] = new_roles
            out[lname] = new_segs
        return out

    def _crossed(self, situation: dict[str, Any]) -> dict[str, Any]:
        """The situation with the spec's interactions layer (from its roles' values), replacing any it had."""
        out = {k: v for k, v in situation.items() if k != INTERACTIONS}
        pairs = {}
        for i in self.interactions:
            value = self._pair_value(out, *i.roles)
            if value is not None:
                pairs[i.name] = value
        if pairs:
            out[INTERACTIONS] = {"pairs": pairs}
        return out

    def _pair_value(self, situation: Mapping[str, Any], a: str, b: str) -> str | None:
        """The two roles' values as one (a number by its bin), or None when either is missing, a set or a text."""
        texts = []
        for path in (a, b):
            lname, sname, rname = path.split(".")
            layer = self._find(self.layers, lname)
            seg = self._find(layer.segments, sname) if layer else None
            role = self._find(seg.roles, rname) if seg else None
            v = situation.get(lname, {}).get(sname, {}).get(rname)
            if role is None or v is None or role.text_encoding or (isinstance(v, list) and role.numeric_config is None):
                return None
            texts.append(kernel._text(v[-1]) if isinstance(v, list) else kernel._text(v))
        return "|".join(texts)

    def restate(self, situation: Mapping[str, Any]) -> dict[str, Any]:
        """A stored situation brought to this spec's word weights and interactions (for Space.rewrite when they
        change)."""
        return self._crossed(self._weighed(dict(situation)))

    recross = restate

    def weights(self, clearance: float = 1.0, present: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The kernel weights tree: every level's similarity_weight, 0 for
        whatever the clearance may not see and, given `present` (a
        situation), 0 for every part it leaves out."""
        if self.security_weight > clearance:
            raise SpecError("", "this model is above the reader's clearance")
        tree: dict[str, Any] = {}
        for l in self.layers:
            lp = (present or {}).get(l.name) if present is not None else None
            lw = 0.0 if l.security_weight > clearance or (present is not None and lp is None) else l.similarity_weight
            segs: dict[str, Any] = {"*": lw}
            for s in l.segments:
                sp = lp.get(s.name) if isinstance(lp, dict) else None
                sw = (
                    0.0
                    if s.security_weight > clearance or (present is not None and sp is None)
                    else s.similarity_weight
                )
                roles: dict[str, Any] = {"*": sw}
                for r in s.roles:
                    gone = present is not None and not (isinstance(sp, dict) and r.name in sp)
                    if r.security_weight > clearance or gone:
                        roles[r.name] = 0.0
                    else:
                        value = sp.get(r.name) if isinstance(sp, dict) else None
                        roles[r.name] = r.similarity_weight * r.value_weight(value)
                segs[s.name] = roles
            tree[l.name] = segs
        if self.interactions:
            ip = (present or {}).get(INTERACTIONS) if present is not None else None
            pairs: dict[str, Any] = {}
            for i in self.interactions:
                value = ip["pairs"].get(i.name) if isinstance(ip, dict) else None
                gone = present is not None and value is None
                if gone or self._security_of(i.roles[0]) > clearance or self._security_of(i.roles[1]) > clearance:
                    pairs[i.name] = 0.0
                else:
                    pairs[i.name] = i.similarity_weight * i.value_weight(value)
            own = max(pairs.values(), default=0.0)
            tree[INTERACTIONS] = {"*": 0.0 if (present is not None and ip is None) else own, "pairs": {"*": 1.0, **pairs}}
        return tree

    def _security_of(self, path: str) -> float:
        lname, sname, rname = path.split(".")
        layer = self._find(self.layers, lname)
        seg = self._find(layer.segments, sname) if layer else None
        role = self._find(seg.roles, rname) if seg else None
        return max(layer.security_weight if layer else 0.0, seg.security_weight if seg else 0.0, role.security_weight if role else 0.0)

    def key(self, situation: Mapping[str, Any]) -> str | None:
        """The record id from its key_part roles, or None if it has none."""
        parts = []
        for l in self.layers:
            for s in l.segments:
                for r in s.roles:
                    if r.key_part:
                        v = situation.get(l.name, {}).get(s.name, {}).get(r.name)
                        if v is None:
                            raise SpecError(f"{l.name}.{s.name}.{r.name}", "a key part is required")
                        parts.append(json.dumps(v, sort_keys=True, ensure_ascii=False))
        if not parts:
            return None
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:12]

    def timestamp(self, data: Mapping[str, Any]) -> float | None:
        """Epoch seconds from the temporal_source role, None for "auto"."""
        if self.temporal_source == "auto":
            return None
        lname, sname, rname = self.temporal_source.split(".")
        v = data.get(lname, {}).get(sname, {}).get(rname)
        if v is None:
            return None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        raise SpecError(self.temporal_source, "a time is epoch seconds or an ISO 8601 string")

    def report(self, tree: dict[str, Any], clearance: float = 1.0) -> dict[str, Any]:
        """A kernel fact tree (FactTree.to_json()["tree"]) cut to what the
        reader may see and to output.report."""
        keep = self.output.report
        out = dict(tree)
        out["children"] = []
        for lnode in tree.get("children", []):
            layer = self._find(self.layers, lnode["key"])
            if layer is None or layer.security_weight > clearance:
                continue
            ln = dict(lnode)
            ln["children"] = []
            for snode in lnode.get("children", []):
                seg = self._find(layer.segments, snode["key"])
                if seg is None or seg.security_weight > clearance:
                    continue
                sn = dict(snode)
                sn["children"] = [
                    rn
                    for rn in snode.get("children", [])
                    if (r := self._find(seg.roles, rn["key"])) is not None
                    and r.security_weight <= clearance
                    and _kept(keep, f"{layer.name}.{seg.name}.{r.name}")
                ]
                if sn["children"]:
                    ln["children"].append(sn)
            if ln["children"]:
                out["children"].append(ln)
        return out

    # -- lookups ----------------------------------------------------

    @staticmethod
    def _find(items: Sequence[Any], name: str) -> Any:
        return next((x for x in items if x.name == name), None)

    def _layer(self, name: str) -> Layer:
        layer = self._find(self.layers, name)
        if layer is None:
            raise SpecError(name, "no such layer")
        return layer

    def _segment(self, layer: Layer, name: str, at: str) -> Segment:
        seg = self._find(layer.segments, name)
        if seg is None:
            raise SpecError(f"{at}.{name}", "no such segment")
        return seg

    def _role(self, seg: Segment, name: str, at: str) -> Role:
        for r in seg.roles:
            if r.name == name or name in r.lexicons:
                return r
        raise SpecError(f"{at}.{name}", "no such role")


def _words_of(value: Any) -> list[str]:
    """The words of a stored bag, weighted or plain; its pairs ("a+b") are not words."""
    items = [x[0] if isinstance(x, list) else x for x in value]
    return [w for w in items if "+" not in w]


def _word_tables(spec: Spec) -> dict[str, tuple[tuple[str, float], ...]]:
    return {
        f"{l.name}.{s.name}.{r.name}": r.value_weights
        for l in spec.layers
        for s in l.segments
        for r in s.roles
        if r.text_encoding
    }


def _entropy(counts: Counter) -> float:
    n = sum(counts.values())
    return -sum(c / n * math.log2(c / n) for c in counts.values() if c) if n else 0.0


def _kept(keep: Sequence[str], path: str) -> bool:
    return not keep or any(path == k or path.startswith(k + ".") for k in keep)


def _value(role: Role, v: Any, path: str) -> Any:
    """One role's value in kernel terms, or None when it carries nothing."""
    if v is None:
        return None
    if role.numeric_config is not None:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise SpecError(path, "a numeric role takes a finite number")
        return role.numeric_config.bins(float(v))
    if role.text_encoding == "bag_of_words":
        if not isinstance(v, str):
            raise SpecError(path, "a bag_of_words role takes a string")
        words = sorted(set(_WORD.findall(v.lower())))
        return words or None
    if isinstance(v, (list, tuple)):
        items = [x for x in v if x is not None]
        for x in items:
            if isinstance(x, (dict, list, tuple)):
                raise SpecError(path, "a set holds scalars")
        return sorted(set(items), key=kernel._text) or None
    if isinstance(v, dict):
        raise SpecError(path, "a role takes a scalar or a set of scalars")
    return v


# ── parsing ─────────────────────────────────────────────────────────


def _fields(obj: Any, path: str, required: Sequence[str], optional: Sequence[str]) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise SpecError(path, "must be an object")
    unknown = sorted(set(obj) - set(required) - set(optional))
    if unknown:
        raise SpecError(path, f"unknown field(s): {', '.join(unknown)}")
    missing = [k for k in required if k not in obj]
    if missing:
        raise SpecError(path, f"missing field(s): {', '.join(missing)}")
    return obj


def _name(v: Any, path: str) -> str:
    if not isinstance(v, str) or not _NAME.match(v):
        raise SpecError(path, "a name is 1-64 letters, digits, _ or -, not starting with a digit")
    return v


def _unit(v: Any, path: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
        raise SpecError(path, "must be a number from 0 to 1")
    return float(v)


def _number(v: Any, path: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise SpecError(path, "must be a finite number")
    return float(v)


def _unique(names: list[str], path: str) -> None:
    seen = set()
    for n in names:
        if n in seen:
            raise SpecError(path, f"'{n}' is named twice")
        seen.add(n)


def _parse_role(obj: Any, path: str) -> Role:
    o = _fields(
        obj,
        path,
        ["name"],
        ["similarity_weight", "security_weight", "key_part", "numeric_config", "text_encoding", "lexicons", "value_weights"],
    )
    name = _name(o["name"], f"{path}.name")
    at = f"{path}({name})"
    numeric = None
    if o.get("numeric_config") is not None:
        n = _fields(o["numeric_config"], f"{at}.numeric_config", ["min_value", "max_value", "bin_width"], [])
        numeric = NumericConfig(
            _number(n["min_value"], f"{at}.numeric_config.min_value"),
            _number(n["max_value"], f"{at}.numeric_config.max_value"),
            _number(n["bin_width"], f"{at}.numeric_config.bin_width"),
        )
        if numeric.max_value <= numeric.min_value or numeric.bin_width <= 0:
            raise SpecError(f"{at}.numeric_config", "needs min_value < max_value and bin_width > 0")
        if (numeric.max_value - numeric.min_value) / numeric.bin_width > 1000:
            raise SpecError(f"{at}.numeric_config", "more than 1000 bins")
    text = o.get("text_encoding")
    if text not in (None, "bag_of_words"):
        raise SpecError(f"{at}.text_encoding", 'is "bag_of_words" or absent')
    if numeric and text:
        raise SpecError(at, "a role is numeric or text, not both")
    lex = o.get("lexicons", [])
    if not isinstance(lex, list):
        raise SpecError(f"{at}.lexicons", "must be a list of names")
    key_part = o.get("key_part", False)
    if not isinstance(key_part, bool):
        raise SpecError(f"{at}.key_part", "must be true or false")
    vw = o.get("value_weights", {})
    if not isinstance(vw, dict) or not all(isinstance(k, str) for k in vw):
        raise SpecError(f"{at}.value_weights", "must be an object of value: weight")
    value_weights = tuple((k, _unit(v, f"{at}.value_weights.{k}")) for k, v in sorted(vw.items()))
    return Role(
        name,
        _unit(o.get("similarity_weight", 1.0), f"{at}.similarity_weight"),
        _unit(o.get("security_weight", 0.0), f"{at}.security_weight"),
        key_part,
        numeric,
        text,
        tuple(_name(x, f"{at}.lexicons") for x in lex),
        value_weights,
    )


def _parse_interactions(obj: Any) -> tuple[Interaction, ...]:
    if not isinstance(obj, list):
        raise SpecError(INTERACTIONS, "must be a list")
    out = []
    for n, io in enumerate(obj):
        at = f"{INTERACTIONS}[{n}]"
        i = _fields(io, at, ["name", "roles"], ["similarity_weight", "value_weights"])
        roles = i["roles"]
        if not isinstance(roles, list) or len(roles) != 2 or not all(isinstance(r, str) for r in roles) or roles[0] == roles[1]:
            raise SpecError(f"{at}.roles", "two different layer.segment.role paths")
        vw = i.get("value_weights", {})
        if not isinstance(vw, dict) or not all(isinstance(k, str) for k in vw):
            raise SpecError(f"{at}.value_weights", "must be an object of value: weight")
        out.append(
            Interaction(
                _name(i["name"], f"{at}.name"),
                (roles[0], roles[1]),
                _unit(i.get("similarity_weight", 1.0), f"{at}.similarity_weight"),
                tuple((k, _unit(v, f"{at}.value_weights.{k}")) for k, v in sorted(vw.items())),
            )
        )
    _unique([i.name for i in out], INTERACTIONS)
    return tuple(out)


def _parse_spec(obj: Any) -> Spec:
    o = _fields(
        obj,
        "",
        ["name", "layers"],
        ["version", "dimension", "seed", "similarity_weight", "security_weight", "temporal_source", "output", INTERACTIONS, "settings"],
    )
    if o.get("version", SPEC_VERSION) != SPEC_VERSION:
        raise SpecError("version", f"this kernel reads spec version {SPEC_VERSION}")
    dim = o.get("dimension", kernel.DIM)
    if isinstance(dim, bool) or not isinstance(dim, int) or not 256 <= dim <= 65536:
        raise SpecError("dimension", "must be an integer from 256 to 65536")
    seed = o.get("seed", 42)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise SpecError("seed", "must be an integer")
    if not isinstance(o["layers"], list) or not o["layers"]:
        raise SpecError("layers", "at least one layer")
    layers = []
    for i, lo in enumerate(o["layers"]):
        lp = f"layers[{i}]"
        l = _fields(lo, lp, ["name", "segments"], ["similarity_weight", "security_weight"])
        if not isinstance(l["segments"], list) or not l["segments"]:
            raise SpecError(f"{lp}.segments", "at least one segment")
        segs = []
        for j, so in enumerate(l["segments"]):
            sp = f"{lp}.segments[{j}]"
            s = _fields(so, sp, ["name", "roles"], ["similarity_weight", "security_weight"])
            if not isinstance(s["roles"], list) or not s["roles"]:
                raise SpecError(f"{sp}.roles", "at least one role")
            roles = tuple(_parse_role(ro, f"{sp}.roles[{k}]") for k, ro in enumerate(s["roles"]))
            _unique([n for r in roles for n in (r.name, *r.lexicons)], f"{sp}.roles")
            segs.append(
                Segment(
                    _name(s["name"], f"{sp}.name"),
                    roles,
                    _unit(s.get("similarity_weight", 1.0), f"{sp}.similarity_weight"),
                    _unit(s.get("security_weight", 0.0), f"{sp}.security_weight"),
                )
            )
        _unique([s.name for s in segs], f"{lp}.segments")
        layers.append(
            Layer(
                _name(l["name"], f"{lp}.name"),
                tuple(segs),
                _unit(l.get("similarity_weight", 1.0), f"{lp}.similarity_weight"),
                _unit(l.get("security_weight", 0.0), f"{lp}.security_weight"),
            )
        )
    _unique([l.name for l in layers], "layers")
    if any(l.name == INTERACTIONS for l in layers):
        raise SpecError("layers", f'"{INTERACTIONS}" is the layer the spec adds for its interactions')
    out = _fields(o.get("output", {}), "output", [], ["report", "costs", "ask"])
    report = out.get("report", [])
    if not isinstance(report, list) or not all(isinstance(x, str) for x in report):
        raise SpecError("output.report", "must be a list of paths")
    costs_in = out.get("costs", {})
    if not isinstance(costs_in, dict) or not all(isinstance(t, dict) for t in costs_in.values()):
        raise SpecError("output.costs", "must be an object: decided outcome -> {true outcome: cost}")
    costs = tuple(
        (str(d), tuple((str(t), _number(c, f"output.costs.{d}.{t}")) for t, c in sorted(table.items())))
        for d, table in sorted(costs_in.items())
    )
    ask = out.get("ask")
    if ask is not None:
        ask = _number(ask, "output.ask")
    spec = Spec(
        _name(o["name"], "name"),
        tuple(layers),
        dim,
        seed,
        _unit(o.get("similarity_weight", 1.0), "similarity_weight"),
        _unit(o.get("security_weight", 0.0), "security_weight"),
        str(o.get("temporal_source", "auto")),
        Output(tuple(report), costs, ask),
        _parse_interactions(o.get(INTERACTIONS, [])),
        Settings().with_changes(_fields(o.get("settings", {}), "settings", [], list(Settings.RANGES))),
    )
    for path in report:
        _resolve(spec, path, "output.report")
    for i in spec.interactions:
        for path in i.roles:
            _resolve(spec, path, f"{INTERACTIONS}.{i.name}.roles")
    if spec.temporal_source != "auto":
        if len(spec.temporal_source.split(".")) != 3:
            raise SpecError("temporal_source", 'is "auto" or a layer.segment.role path')
        _resolve(spec, spec.temporal_source, "temporal_source")
    return spec


def _resolve(spec: Spec, path: str, at: str) -> None:
    parts = path.split(".")
    if not 1 <= len(parts) <= 3:
        raise SpecError(at, f"'{path}' is not a layer, layer.segment or layer.segment.role path")
    layer = spec._find(spec.layers, parts[0])
    seg = spec._find(layer.segments, parts[1]) if layer and len(parts) > 1 else layer
    role = spec._find(seg.roles, parts[2]) if seg and len(parts) > 2 else seg
    if role is None:
        raise SpecError(at, f"'{path}' names nothing in the spec")


# ── a model: a spec over a Space ────────────────────────────────────


class Model:
    """One model: its spec and its precedents. Data goes in the spec's
    shape; reads come back as fact trees in the spec's shape."""

    def __init__(
        self,
        spec: Spec,
        *,
        author: str = "anon",
        path: str | None = None,
        tau: float | None = None,
        k: int | None = None,
        min_sim: float | None = None,
        refresh_every: int | None = None,
        readonly: bool = False,
    ):
        """The levers come from the spec's `settings`, then from what configure() kept with the store, then from
        the arguments given here (k, min_sim, refresh_every), each over the last. readonly opens the store
        read-only (kernel.Space's readonly): query, facts, probabilities, decide and the settings read as ever;
        record, learn, calibrate, refresh and configure raise kernel.ReadOnlyError (E_READ_ONLY) before
        anything is written, and a restatement on open stays in memory."""
        self.given = spec
        self.readonly = bool(readonly)
        self.space = kernel.Space(
            dim=spec.dimension,
            k=spec.settings.k,
            min_sim=spec.settings.min_sim,
            seed=spec.seed,
            author=author,
            path=path,
            tau=tau,
            readonly=readonly,
        )
        kept = self.space.meta("settings")
        self.overrides: dict[str, Any] = dict(kept) if isinstance(kept, dict) else {}
        given = {k_: v for k_, v in (("k", k), ("min_sim", min_sim), ("refresh_every", refresh_every)) if v is not None}
        self.settings = spec.settings.with_changes(self.overrides).with_changes(given)
        self.spec = spec.with_settings(self.settings)
        learned = self.space.meta("learned")
        self.evidence: dict[str, Any] = {}
        if isinstance(learned, dict):
            self.spec = self.spec.with_weights(learned)
            self.evidence = learned.get("~evidence", {})
        self._apply()
        if len(self.space):
            # a store recorded under other settings: its bags brought to these (in memory only, read-only)
            self.space.rewrite(self.spec.restate, persist=not self.readonly)

    def _apply(self) -> None:
        self.space.k, self.space.min_sim = self.settings.k, self.settings.min_sim
        self.refresh_every = self.settings.refresh_every
        try:
            from glyphh_lexicon import LEXICON

            LEXICON.enabled = self.settings.lexicon
        except ImportError:  # no lexicon module shipped: words are symbols either way
            pass

    def configure(self, **changes: Any) -> dict[str, Any]:
        """Change the levers live: checked, kept with the store (over the spec's `settings`, until changed again),
        and a learning lever moved relearns the store there and then (when it has learned). Returns every
        setting as it now stands. Read-only, configure() with no changes reads them; any change is refused."""
        if self.readonly:
            if changes:
                raise kernel.ReadOnlyError("configure", self.space.path)
            return self.settings.to_json(all=True)
        new = self.settings.with_changes(changes)
        moved = [name for name in Settings.LEARNING if getattr(new, name) != getattr(self.settings, name)]
        self.settings = new
        self.overrides.update(changes)
        self.space.set_meta("settings", self.overrides)
        self.spec = self.spec.with_settings(new)
        self._apply()
        if moved and self.space.meta("learned") is not None:
            self.learn()
        return self.settings.to_json(all=True)

    def _writes(self, operation: str) -> None:
        if self.readonly:
            raise kernel.ReadOnlyError(operation, self.space.path)

    def record(self, data: Any, outcome: str | None = None, ts: float | None = None) -> str:
        self._writes("record")
        situation = self.spec.situation(data)
        stamp = ts if ts is not None else self.spec.timestamp(data)
        rid = self.space.record(situation, outcome, rid=self.spec.key(situation), ts=stamp)
        if outcome is not None and self.refresh_every and len(self.space.graded()) % self.refresh_every == 0:
            try:
                self.refresh()
            except SpecError:  # one outcome so far: nothing to learn yet
                pass
        return rid

    def learn(self) -> dict[str, dict[str, Any]]:
        """Learn the weights from the store's own graded precedents (every role's similarity_weight and
        value_weights, as Spec.learned does) and keep them with the store, over the spec file's, until the next
        learn. Needs at least ten graded precedents with two outcomes. Returns the learned table."""
        self._writes("learn")
        graded = self.space.graded()
        if len(graded) < 10:
            raise SpecError("", "learn needs at least 10 precedents with an outcome")
        learned = self.given.with_settings(self.settings).learn(graded)
        before = self.spec
        self.spec = self.given.with_settings(self.settings).with_weights(learned)
        self.evidence = learned.get("~evidence", {})
        self.space.set_meta("learned", learned)
        if self.spec.interactions != before.interactions or _word_tables(self.spec) != _word_tables(before):
            self.space.rewrite(self.spec.restate)  # every precedent gets the words' weights and the pairs the spec now reads
        return learned

    def refresh(self, sample: int | None = None, clearance: float = 1.0, by: str | None = None) -> dict[str, Any]:
        """learn() then calibrate(): the weights and the temperature follow the store. Returns both."""
        self._writes("refresh")
        learned = self.learn()
        return {"learned": learned, "tau": self.calibrate(sample, clearance, by)}

    def bias(self, situation: Mapping[str, Any]) -> Callable[[str], float] | None:
        """What the situation's words say of each outcome, as a factor on a vote: exp(word_evidence × the mean
        pointwise mutual information of the words with the outcome), over the text roles the store has evidence
        for; None when there is none or the lever is 0."""
        strength = self.settings.word_evidence
        if strength <= 0 or not self.evidence:
            return None
        totals: dict[str, float] = defaultdict(float)
        seen = 0
        for path, table in self.evidence.items():
            lname, sname, rname = path.split(".")
            value = situation.get(lname, {}).get(sname, {}).get(rname)
            if value is None or not table.get("total"):
                continue
            n = table["total"]
            for word in set(_words_of(value)):
                counts = table["words"].get(word)
                if counts is None:
                    continue
                seen += 1
                p_word = sum(counts.values()) / n
                for outcome, n_outcome in table["outcomes"].items():
                    expected = n_outcome * p_word
                    totals[outcome] += math.log((counts.get(outcome, 0) + 0.1) / (expected + 0.1))
        if not seen:
            return None
        factor = {o: math.exp(strength * max(min(t / seen, 3.0), -3.0)) for o, t in totals.items()}
        return lambda outcome: factor.get(outcome, 1.0)

    def query(self, data: Any, clearance: float = 1.0) -> kernel.Answer:
        situation = self.spec.situation(data)
        return self.space.query(situation, weights=self.spec.weights(clearance, situation), bias=self.bias(situation))

    def facts(
        self, data: Any, clearance: float = 1.0, top: int | None = None, min_score: float | None = None
    ) -> list[dict[str, Any]]:
        """The fact trees, best first, each cut to what the reader may see."""
        situation = self.spec.situation(data)
        trees = self.space.facts(
            situation, weights=self.spec.weights(clearance, situation), top=top, min_score=min_score
        )
        return [{**t.to_json(), "tree": self.spec.report(t.to_json()["tree"], clearance)} for t in trees]

    def probabilities(self, data: Any, clearance: float = 1.0) -> dict[str, float]:
        """P(outcome | data) over the nearest precedents, at the store's temperature (calibrate() fits it)."""
        situation = self.spec.situation(data)
        return self.space.probabilities(
            situation, weights=self.spec.weights(clearance, situation), bias=self.bias(situation)
        )

    def calibrate(self, sample: int | None = None, clearance: float = 1.0, by: str | None = None) -> float:
        """Fit the store's temperature on its own newest precedents, each held out in turn and scored with the
        weights the spec gives its situation. `by` is "loss" (the least log-loss: probabilities that mean what
        they say) or "cost" (the least cost of deciding by output.costs, log-loss breaking ties: probabilities
        that decide best). Kept in the store. Returns tau."""
        self._writes("calibrate")
        by = by or self.settings.calibrate_by
        sample = sample or self.settings.calibrate_sample
        if by not in ("loss", "cost"):
            raise SpecError("", 'calibrate by "loss" or "cost"')
        return self.space.calibrate(
            sample,
            weights_for=lambda s: self.spec.weights(clearance, s),
            cost=(lambda p, true: self.spec.output.cost(self._least(p), true)) if by == "cost" else None,
            bias_for=self.bias,
        )

    def _least(self, probabilities: Mapping[str, float]) -> str:
        expected = {
            decided: sum(p * self.spec.output.cost(decided, true) for true, p in probabilities.items())
            for decided in probabilities
        }
        return min(expected, key=lambda o: (expected[o], o))

    def decide(self, data: Any, clearance: float = 1.0) -> dict[str, Any]:
        """The outcome with the least expected cost under the spec's output.costs (every mistake costs 1 when the
        spec names none), from the calibrated probabilities. `ask` is true when a person should answer instead:
        nothing is known, or the spec prices asking (output.ask) below every outcome's expected cost; the outcome
        is then still the best guess, for the person to see."""
        probabilities = self.probabilities(data, clearance)
        if not probabilities:
            return {"outcome": None, "expected_cost": None, "probabilities": {}, "ask": True}
        expected = {
            decided: sum(p * self.spec.output.cost(decided, true) for true, p in probabilities.items())
            for decided in probabilities
        }
        best = self._least(probabilities)
        ask = self.spec.output.ask
        return {
            "outcome": best,
            "expected_cost": round(expected[best], 4),
            "probabilities": {o: round(p, 4) for o, p in probabilities.items()},
            "ask": ask is not None and expected[best] > ask,
        }

    def __len__(self) -> int:
        return len(self.space)
