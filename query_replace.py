"""
query_replace.py — per-title query rewriting for the Easynews indexer.

Sits in front of the generic Norwegian transliteration: maps a specific incoming
search TITLE to the exact form a release is posted under, for cases the generic
ASCII fold can't cover or where upstream (AIOStreams) has already mangled the
title. Applied to the title only; season/episode/year are appended by the caller
afterwards (or protected here if a client embedded them), so they always carry
over.

Configure with EASYNEWS_QUERY_REPLACE. Two accepted formats:

  Simple (easiest in .env) — rules separated by ';;' or newlines, 'match => to':
    EASYNEWS_QUERY_REPLACE="ikke lov a le pa hytta => Ikke lov aa le paa hytta ;; norsemen => Vikingane"

  JSON (for awkward characters / explicit ordering):
    EASYNEWS_QUERY_REPLACE=[{"match":"norsemen","to":"Vikingane"}]

Match syntax. Matching is CASE-INSENSITIVE. Write keys in the folded/lowercased
form the query actually arrives in (AIOStreams lowercases and ASCII-folds, so
"å"->"a", German "ü"->"ue", etc.):

  norsemen                  substring  -> that phrase is replaced by `to`
  temptation island*        starts-with-> whole title becomes `to`, or the
                                          literal "temptation island" if `to` empty
  *verfuehrung im paradies  ends-with  -> whole title becomes `to`, or the matched
                                          suffix literal if `to` empty
  *paradies*                contains   -> same, for a phrase anywhere in the title

Rules are tried in order; the FIRST match wins. Put more specific rules first.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)

# A rule is (core, to, anchor). anchor in {"substr","prefix","suffix","contains"}.
Rule = Tuple[str, str, str]

# Trailing release metadata to protect from truncation even if a client embeds it
# in q (e.g. "title s06e03"). Season/episode/quality only — NOT a bare year, so we
# never mistake titles like "1923" or "Blade Runner 2049" for metadata.
_TAIL_RE = re.compile(
    r"\s+(?=(?:s\d{1,2}(?:e\d{1,3})?|e\d{1,3}|\d{3,4}p)(?:\b|$))",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def _split_title_tail(text: str) -> Tuple[str, str]:
    """Split 'Title S06E03' into ('Title', ' S06E03'). Tail keeps its leading
    space so re-joining is just head + tail. No metadata -> ('Title', '')."""
    m = _TAIL_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()], text[m.start():]


def _parse_rule(match_raw: str, to_raw: str) -> Rule | None:
    to = to_raw.strip()
    m = match_raw.strip()
    lead = m.startswith("*")
    trail = m.endswith("*")
    core = m.strip("*").strip()
    if not core:
        return None
    if lead and trail:
        anchor = "contains"
    elif trail:
        anchor = "prefix"
    elif lead:
        anchor = "suffix"
    else:
        anchor = "substr"
    return (core, to, anchor)


def parse_rules(raw: str) -> List[Rule]:
    raw = (raw or "").strip()
    if not raw:
        return []
    pairs: List[Tuple[str, str]] = []
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("EASYNEWS_QUERY_REPLACE: invalid JSON (%s); ignoring", e)
            return []
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, dict):
                pairs.append((str(entry.get("match", "")), str(entry.get("to", ""))))
    else:
        for chunk in re.split(r"(?:\r?\n|;;)", raw):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=>" not in chunk:
                logger.warning(
                    "EASYNEWS_QUERY_REPLACE: rule %r has no '=>'; skipping", chunk
                )
                continue
            m_raw, to_raw = chunk.split("=>", 1)
            pairs.append((m_raw, to_raw))
    rules: List[Rule] = []
    for m_raw, to_raw in pairs:
        rule = _parse_rule(m_raw, to_raw)
        if rule:
            rules.append(rule)
    return rules


def apply_rules(title: str, rules: List[Rule]) -> str:
    """Rewrite the title per the first matching rule. Returns title unchanged if
    nothing matches. Trailing SxxExx/quality is protected and re-appended."""
    if not rules or not title:
        return title
    head, tail = _split_title_tail(title)
    low = head.lower()
    for core, to, anchor in rules:
        c = core.lower()
        if anchor == "prefix":
            if low.startswith(c):
                head = to or core
                break
        elif anchor == "suffix":
            if low.endswith(c):
                head = to or core
                break
        elif anchor == "contains":
            if c in low:
                head = to or core
                break
        else:  # substr: word-bounded replace of the phrase wherever it appears
            pat = re.compile(r"\b" + re.escape(core) + r"\b", re.IGNORECASE)
            if pat.search(head):
                head = pat.sub(to, head)
                break
    head = _WS_RE.sub(" ", head).strip()
    return (head + tail) if head else title


# --- standalone smoke test: `python query_replace.py` -----------------------
if __name__ == "__main__":
    rules = parse_rules(
        "ikke lov a le pa hytta => Ikke lov aa le paa hytta ;; "
        "norsemen => Vikingane ;; "
        "temptation island* => ;; "
        "*verfuehrung im paradies =>"
    )
    cases = [
        "ikke lov a le pa hytta",                              # title only (episode via params)
        "ikke lov a le pa hytta s06e05",                       # episode embedded in q
        "norsemen",
        "norsemen s03e01",
        "temptation island - verfuehrung im paradies",
        "temptation island - verfuehrung im paradies s03e04",  # star keeps episode
        "the office",                                          # no rule -> unchanged
    ]
    for t in cases:
        print(f"{t!r:55} -> {apply_rules(t, rules)!r}")
