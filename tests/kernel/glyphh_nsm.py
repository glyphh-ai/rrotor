"""
The NSM default model — text in, with no model of its own.

Every organization has one model on this spec: the Natural Semantic
Metalanguage primes, the closed vocabulary every language shares (I, YOU,
KNOW, WANT, GOOD, BAD, BEFORE, BECAUSE, ...). Text is taken down to the
primes it relates to, deterministically, and recorded as:

    {"nsm":  {<group>: {<PRIME>: true, ...}, ...},   the meaning
     "text": {"words": {"words": <the text>}}}       the wording

so a query matches by what the text is about and, more lightly, by the
words it uses. Ported from rrotor's primes.ts (glyphh_rotor/primes).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from glyphh_spec import Spec

PRIMES: Dict[str, List[str]] = {
    "substantives": ["I", "YOU", "SOMEONE", "SOMETHING", "PEOPLE", "BODY"],
    "relational": ["KIND", "PART"],
    "determiners": ["THIS", "SAME", "OTHER"],
    "quantifiers": ["ONE", "TWO", "SOME", "ALL", "MUCH", "LITTLE"],
    "evaluators": ["GOOD", "BAD"],
    "descriptors": ["BIG", "SMALL"],
    "mental": ["THINK", "KNOW", "WANT", "DONT_WANT", "FEEL", "SEE", "HEAR"],
    "speech": ["SAY", "WORDS", "TRUE"],
    "actions": ["DO", "HAPPEN", "MOVE"],
    "existence": ["BE_SOMEWHERE", "THERE_IS", "BE_SOMEONE"],
    "life": ["LIVE", "DIE"],
    "time": ["WHEN", "NOW", "BEFORE", "AFTER", "A_LONG_TIME", "A_SHORT_TIME", "FOR_SOME_TIME", "MOMENT"],
    "space": ["WHERE", "HERE", "ABOVE", "BELOW", "FAR", "NEAR", "SIDE", "INSIDE", "TOUCH"],
    "logic": ["NOT", "MAYBE", "CAN", "BECAUSE", "IF"],
    "intensifier": ["VERY", "MORE"],
    "similarity": ["LIKE"],
}
GROUP_OF: Dict[str, str] = {p: g for g, ps in PRIMES.items() for p in ps}

# A hit means the word plausibly RELATES to the prime, nothing stronger.
KEYWORDS: Dict[str, str] = {
    # mental predicates
    **dict.fromkeys(["know", "knew", "remember", "recall", "forget", "learn", "understand"], "KNOW"),
    **dict.fromkeys(["think", "thought", "believe", "idea", "plan", "decide", "consider"], "THINK"),
    **dict.fromkeys(["want", "need", "wish", "hope", "goal", "prefer", "intend"], "WANT"),
    **dict.fromkeys(["feel", "felt", "love", "hate", "afraid", "happy", "sad", "angry", "worry", "excited"], "FEEL"),
    **dict.fromkeys(["see", "saw", "look", "watch", "show"], "SEE"),
    **dict.fromkeys(["hear", "heard", "listen", "sound"], "HEAR"),
    # speech
    **dict.fromkeys(["say", "said", "tell", "told", "ask", "asked", "talk", "call", "name"], "SAY"),
    "word": "WORDS",
    **dict.fromkeys(["true", "false", "correct", "wrong", "fact"], "TRUE"),
    # actions and events
    **dict.fromkeys(["do", "did", "make", "made", "build", "built", "create", "work", "run", "use"], "DO"),
    **dict.fromkeys(["happen", "happened", "occur", "event", "became"], "HAPPEN"),
    **dict.fromkeys(["move", "go", "went", "come", "leave", "arrive", "travel", "drive"], "MOVE"),
    # people and things
    **dict.fromkeys(["i", "me", "my", "mine", "myself"], "I"),
    **dict.fromkeys(["you", "your"], "YOU"),
    **dict.fromkeys(["people", "family", "team", "children"], "PEOPLE"),
    **dict.fromkeys(["person", "who", "someone", "somebody", "wife", "husband", "friend", "kid", "child", "son",
                     "daughter", "mother", "father"], "SOMEONE"),
    **dict.fromkeys(["something", "thing", "what"], "SOMETHING"),
    **dict.fromkeys(["body", "hand", "head", "health"], "BODY"),
    # evaluation and description
    **dict.fromkeys(["good", "great", "best", "right", "better", "favorite"], "GOOD"),
    **dict.fromkeys(["bad", "worst", "worse", "problem", "broken", "fail", "bug", "error"], "BAD"),
    **dict.fromkeys(["big", "large", "huge"], "BIG"),
    **dict.fromkeys(["small", "tiny"], "SMALL"),
    "little": "LITTLE",
    # time
    **dict.fromkeys(["when", "time", "date", "year", "month", "week", "day", "schedule"], "WHEN"),
    **dict.fromkeys(["now", "today", "currently"], "NOW"),
    **dict.fromkeys(["before", "ago", "yesterday", "was", "were", "past", "history"], "BEFORE"),
    **dict.fromkeys(["after", "tomorrow", "next", "will", "future", "soon"], "AFTER"),
    **dict.fromkeys(["always", "forever"], "A_LONG_TIME"),
    # space
    **dict.fromkeys(["where", "place", "location", "address", "city", "home", "house"], "WHERE"),
    "here": "HERE",
    **dict.fromkeys(["near", "close"], "NEAR"),
    "far": "FAR",
    **dict.fromkeys(["inside", "in"], "INSIDE"),
    # existence and life
    **dict.fromkeys(["is", "are", "exist", "have", "has", "own"], "THERE_IS"),
    **dict.fromkeys(["live", "life", "born"], "LIVE"),
    **dict.fromkeys(["die", "died", "death", "dead"], "DIE"),
    # quantity
    "one": "ONE", "two": "TWO",
    **dict.fromkeys(["some", "few"], "SOME"),
    **dict.fromkeys(["all", "every", "everything"], "ALL"),
    **dict.fromkeys(["many", "much", "most", "lot", "count", "number"], "MUCH"),
    # logic and modality
    **dict.fromkeys(["not", "no", "none", "without", "never", "cannot"], "NOT"),
    **dict.fromkeys(["maybe", "might", "perhaps", "could", "possibly"], "MAYBE"),
    **dict.fromkeys(["can", "able"], "CAN"),
    **dict.fromkeys(["because", "why", "reason", "since", "cause"], "BECAUSE"),
    **dict.fromkeys(["if", "unless", "whether"], "IF"),
    **dict.fromkeys(["very", "really", "extremely"], "VERY"),
    **dict.fromkeys(["more", "less"], "MORE"),
    **dict.fromkeys(["like", "similar", "as"], "LIKE"),
    "same": "SAME",
    **dict.fromkeys(["different", "other", "another"], "OTHER"),
    "this": "THIS",
    **dict.fromkeys(["kind", "type"], "KIND"),
    "part": "PART",
}
_WORD = re.compile(r"[a-z']+")
_SUFFIXES = ("ing", "ed", "es", "s")


def _lookup(word: str) -> str | None:
    if word in KEYWORDS:
        return KEYWORDS[word]
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            stem = word[: -len(suffix)]
            hit = KEYWORDS.get(stem) or KEYWORDS.get(stem + "e")
            if hit:
                return hit
    return None


def decompose(text: str) -> List[str]:
    """The primes a text relates to, in order of first appearance. A digit
    anywhere relates to MUCH. Deterministic: the same text, the same primes."""
    seen: Dict[str, None] = {}
    for word in _WORD.findall(text.lower()):
        prime = _lookup(word)
        if prime:
            seen.setdefault(prime)
    if any(c.isdigit() for c in text):
        seen.setdefault("MUCH")
    return list(seen)


SPEC: Spec = Spec.from_json({
    "name": "nsm",
    "layers": [
        {"name": "nsm", "similarity_weight": 1.0, "segments": [
            {"name": group, "roles": [{"name": p} for p in primes]} for group, primes in PRIMES.items()]},
        {"name": "text", "similarity_weight": 0.5, "segments": [
            {"name": "words", "roles": [{"name": "words", "text_encoding": "bag_of_words"}]}]},
    ],
})


def data(text: str) -> Dict[str, Any]:
    """A text as data in the NSM spec's shape."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is a non-empty string")
    meaning: Dict[str, Dict[str, bool]] = {}
    for prime in decompose(text):
        meaning.setdefault(GROUP_OF[prime], {})[prime] = True
    out: Dict[str, Any] = {"text": {"words": {"words": text}}}
    if meaning:
        out["nsm"] = meaning
    return out
