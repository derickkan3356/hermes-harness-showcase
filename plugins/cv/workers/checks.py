"""The draft checks. Each returns plain statements for the agent; none blocks.

What a fixed rule can say reliably is said here: a number that is not in the
facts a bullet cites, a name that is not in them, a JD term that is not in the
CV text, a banned word, an em dash, first person. Whether the wording still
fits is the agent's call, and the user's.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
}
_WORD_NUMBER = re.compile(r"\b(" + "|".join(_WORDS) + r")\b", re.IGNORECASE)

# A word that looks like a name: an inner capital, a digit, or all capitals.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.&/-]*[A-Za-z0-9+#]|[A-Za-z]")

BANNED = [
    "spearheaded", "spearheading", "leveraged", "leveraging", "leverage", "passionate",
    "cutting-edge", "seamless", "seamlessly", "robust", "innovative", "results-driven",
    "synergy", "synergies", "utilized", "utilised", "utilize", "utilise", "state-of-the-art",
    "world-class", "best-in-class", "game-changer", "game-changing", "dynamic", "orchestrated",
    "revolutionized", "revolutionised", "transformative", "empowered", "harnessed",
    "proven track record", "go-getter", "detail-oriented", "self-starter", "thought leader",
    "delve", "showcasing", "pivotal", "meticulous", "elevated",
]
_BANNED = re.compile(r"\b(" + "|".join(re.escape(w) for w in BANNED) + r")\b", re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(I|me|my|myself)\b")


def numbers(text: str) -> Set[float]:
    found: Set[float] = set()
    for raw in _NUMBER.findall(text):
        # 2,000 is two thousand; 3.9 is three point nine.
        plain = re.sub(r",(?=\d{3}\b)", "", raw).replace(",", ".")
        try:
            found.add(float(plain))
        except ValueError:
            for part in re.split(r"[.,]", raw):
                found.add(float(part))
    for word in _WORD_NUMBER.findall(text):
        found.add(float(_WORDS[word.lower()]))
    return found


def _fmt(n: float) -> str:
    return str(int(n)) if n == int(n) else str(n)


def unsupported_numbers(text: str, sources: Iterable[str], extra: Iterable[float] = ()) -> List[str]:
    allowed: Set[float] = set(extra)
    for s in sources:
        allowed |= numbers(s)
    return sorted({_fmt(n) for n in numbers(text) if n not in allowed}, key=lambda s: float(s))


def _in_pool(tok: str, pool: str) -> bool:
    """Every part of a joined name ("PostgreSQL/pgvector", "second-LLM") is in the facts, plural or not."""
    for part in re.split(r"[/-]", tok.lower()):
        if part and part not in pool and not (part.endswith("s") and part[:-1] in pool):
            return False
    return True


def unsupported_names(text: str, sources: Iterable[str]) -> List[str]:
    pool = " ".join(sources).lower()
    out: List[str] = []
    for i, m in enumerate(_TOKEN.finditer(text)):
        tok = m.group(0).rstrip(".")
        inner_cap = any(c.isupper() for c in tok[1:])
        looks_named = inner_cap or any(c.isdigit() for c in tok) or (tok.isupper() and len(tok) > 1) \
            or (i > 0 and tok[0].isupper())
        if not looks_named or _in_pool(tok, pool):
            continue
        if tok not in out:
            out.append(tok)
    return out


def normalise(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[-_/]", " ", text.lower())).strip() + " "


def jd_gaps(terms: Iterable[str], cv_text: str) -> List[str]:
    hay = normalise(re.sub(r"[^\w\s/+#.-]", " ", cv_text))
    gaps = []
    for t in terms:
        needle = normalise(re.sub(r"[^\w\s/+#.-]", " ", t)).strip()
        if needle and not re.search(r"(?<![\w])" + re.escape(needle) + r"(?![\w])", hay):
            gaps.append(t)
    return gaps


def style(text: str) -> List[str]:
    notes = []
    banned = sorted({w.lower() for w in _BANNED.findall(text)})
    if banned:
        notes.append("banned word: " + ", ".join(banned))
    if "—" in text:
        notes.append("em dash")
    if _FIRST_PERSON.search(text):
        notes.append("first person")
    return notes


def skill_gaps(items: Iterable[str], master_skills: Dict[str, List[str]]) -> List[str]:
    known = {normalise(s).strip() for group in master_skills.values() for s in group}
    return [i for i in items if normalise(i).strip() not in known]
