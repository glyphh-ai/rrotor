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

import hashlib
import json
import math
from collections import Counter, defaultdict
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import glyphh_kernel as kernel

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
class Output:
    report: tuple[str, ...] = ()
    # the cost of deciding one outcome when another is true: costs[decided][true]; a pair not named costs 0
    # when they are the same outcome and 1 otherwise. decide() picks the outcome with the least expected cost.
    costs: tuple[tuple[str, tuple[tuple[str, float], ...]], ...] = ()

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
            },
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
        of a role weighs 1). Layers' and segments' weights are left as they are; so are security weights."""
        base = Counter(outcome for _, outcome in cases)
        if len(base) < 2:
            raise SpecError("", "learn needs cases with at least two outcomes")
        h_base = _entropy(base)
        gains: dict[str, float] = {}
        moves: dict[str, dict[str, float]] = {}
        situations = [(self.situation(data), outcome) for data, outcome in cases]
        base_rate = {o: n / sum(base.values()) for o, n in base.items()}
        for layer in self.layers:
            for seg in layer.segments:
                for role in seg.roles:
                    path = f"{layer.name}.{seg.name}.{role.name}"
                    groups: dict[str, Counter] = defaultdict(Counter)
                    for situation, outcome in situations:
                        value = situation.get(layer.name, {}).get(seg.name, {}).get(role.name)
                        if value is None or role.key_part or role.text_encoding:
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
        top = max(gains.values(), default=0.0)
        obj = self.to_json()
        for layer in obj["layers"]:
            for seg in layer["segments"]:
                for role in seg["roles"]:
                    path = f"{layer['name']}.{seg['name']}.{role['name']}"
                    if path in gains and top > 0:
                        role["similarity_weight"] = round(gains[path] / top, 4)
                        best = max(moves[path].values(), default=0.0)
                        if best > 0:
                            role["value_weights"] = {k: round(m / best, 4) for k, m in moves[path].items()}
        return Spec.from_json(obj)

    @property
    def spec_id(self) -> str:
        """Content address: the same spec gives the same id anywhere."""
        text = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
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
        return out

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
        return tree

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


def _parse_spec(obj: Any) -> Spec:
    o = _fields(
        obj,
        "",
        ["name", "layers"],
        ["version", "dimension", "seed", "similarity_weight", "security_weight", "temporal_source", "output"],
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
    out = _fields(o.get("output", {}), "output", [], ["report", "costs"])
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
    spec = Spec(
        _name(o["name"], "name"),
        tuple(layers),
        dim,
        seed,
        _unit(o.get("similarity_weight", 1.0), "similarity_weight"),
        _unit(o.get("security_weight", 0.0), "security_weight"),
        str(o.get("temporal_source", "auto")),
        Output(tuple(report), costs),
    )
    for path in report:
        _resolve(spec, path, "output.report")
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
        k: int = 15,
        min_sim: float = 0.10,
        author: str = "anon",
        path: str | None = None,
        tau: float | None = None,
    ):
        self.spec = spec
        self.space = kernel.Space(
            dim=spec.dimension, k=k, min_sim=min_sim, seed=spec.seed, author=author, path=path, tau=tau
        )

    def record(self, data: Any, outcome: str | None = None, ts: float | None = None) -> str:
        situation = self.spec.situation(data)
        stamp = ts if ts is not None else self.spec.timestamp(data)
        return self.space.record(situation, outcome, rid=self.spec.key(situation), ts=stamp)

    def query(self, data: Any, clearance: float = 1.0) -> kernel.Answer:
        situation = self.spec.situation(data)
        return self.space.query(situation, weights=self.spec.weights(clearance, situation))

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
        return self.space.probabilities(situation, weights=self.spec.weights(clearance, situation))

    def calibrate(self, sample: int = 100, clearance: float = 1.0) -> float:
        """Fit the store's temperature on its own newest precedents, each held out in turn and scored with the
        weights the spec gives its situation. Kept in the store. Returns tau."""
        return self.space.calibrate(sample, weights_for=lambda s: self.spec.weights(clearance, s))

    def decide(self, data: Any, clearance: float = 1.0) -> dict[str, Any]:
        """The outcome with the least expected cost under the spec's output.costs (every mistake costs 1 when the
        spec names none), from the calibrated probabilities; with none, nothing is decided."""
        probabilities = self.probabilities(data, clearance)
        if not probabilities:
            return {"outcome": None, "expected_cost": None, "probabilities": {}}
        expected = {
            decided: sum(p * self.spec.output.cost(decided, true) for true, p in probabilities.items())
            for decided in probabilities
        }
        best = min(expected, key=lambda o: (expected[o], o))
        return {
            "outcome": best,
            "expected_cost": round(expected[best], 4),
            "probabilities": {o: round(p, 4) for o, p in probabilities.items()},
        }

    def __len__(self) -> int:
        return len(self.space)
