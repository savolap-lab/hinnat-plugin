"""Machine-readable site glossary: rules/sanasto.yaml -> request translation.

rules/preferences.md explains the words to people. This file makes the same
words work without anybody reading anything: price_bom.py and find.py
translate a request through it before searching, so "6 mmj" prices as
MMJ 5x6 on every machine, not only where the model happened to read the
prose carefully.

The file is a flat `"word": "target"` map. It is valid YAML, but it is read
here with a regex so the query side stays standard library only and needs no
build step -- a word committed by one buyer works for the next one on pull.

  key     lowercase, may hold one {n} placeholder for a number ("{n} mmj")
  target  what to search instead: a sähkönumero, several joined with | when
          the same product has different codes per supplier, a cable spec,
          or plain search words. {n} is carried over.
"""
import os
import re

from . import paths

ROOT = paths.ROOT
PATH = paths.SANASTO[0]

LINE_RE = re.compile(r'^\s*"([^"]+)"\s*:\s*"([^"]*)"\s*(?:#.*)?$')
NUM = r"(?P<n>\d+(?:[.,]\d+)?)"
_cache = {}


def _norm(s):
    return re.sub(r"\s+", " ", s.strip().lower())


def parse(text):
    """-> [(compiled key, target, key as written)]. A bad line is an error:
    a glossary line that silently does nothing is a word nobody taught."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            raise ValueError(
                f'sanasto.yaml line {no}: expected "word": "target", got {line!r}')
        key, target = _norm(m.group(1)), m.group(2).strip()
        pat = re.escape(key).replace(re.escape("{n}"), NUM)
        out.append((re.compile(pat), target, key))
    return out


def load(path=None):
    """One file, or with no argument every glossary in paths.SANASTO in order
    (organisation first, then the plugin's general slang)."""
    if path is None:
        return [e for p in paths.SANASTO for e in load(p)]
    if not os.path.exists(path):
        return []
    stamp = os.path.getmtime(path)
    if _cache.get(path, (None,))[0] != stamp:
        with open(path, encoding="utf-8") as fh:
            _cache[path] = (stamp, parse(fh.read()))
    return _cache[path][1]


def translate(request, entries):
    """-> (target, key) for the first entry matching the WHOLE request, else None."""
    q = _norm(request)
    for pat, target, key in entries:
        m = pat.fullmatch(q)
        if m:
            n = (m.groupdict().get("n") or "").replace(",", ".")
            return target.replace("{n}", n), key
    return None
