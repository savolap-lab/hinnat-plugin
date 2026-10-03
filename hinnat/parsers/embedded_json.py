"""Pull a JSON value out of a server-rendered page.

Onninen has no stock XHR. The product page is server-rendered and the
availability data is already inside the HTML document — the same JSON the
adapter would have received from an endpoint, just delivered with the page.

So this is not "scraping the rendered page". We are not reading numbers out of
the DOM, where a restyle would break us. We locate a named key in the embedded
payload and hand the value to the existing adapter, which still raises if the
schema moves. What changes is the transport, not the contract.

Standard library only.
"""
import json
import re

OPENERS = {"[": "]", "{": "}"}


class ExtractError(ValueError):
    pass


def _scan(text, start):
    """Return the balanced JSON value beginning at text[start].

    A brace counter that is not string-aware will stop early on a product name
    containing a bracket, so quotes and escapes are tracked explicitly.
    """
    opener = text[start]
    if opener not in OPENERS:
        raise ExtractError(f"expected a JSON array or object, got {opener!r}")
    closer = OPENERS[opener]
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise ExtractError("unterminated JSON value — the page was truncated")


def _unescape(text):
    r"""Undo one level of backslash escaping.

    Frameworks commonly embed the payload as a JSON *string* inside a script
    tag, so the document contains \"availability\":[... rather than
    "availability":[... . The scanner cannot track string state through that —
    every \" looks like an escaped quote inside a string, so it never finds
    the closing bracket. Unescaping the whole document first and rescanning is
    simpler and correct; json.loads still validates whatever comes out.
    """
    return text.replace('\\"', '"').replace("\\\\", "\\")


def _offsets(text, key):
    """Offsets of the value that follows each occurrence of `key`."""
    for m in re.finditer(r'"%s"\s*:\s*' % re.escape(key), text):
        j = m.end()
        if j < len(text) and text[j] in OPENERS:
            yield j


def extract(text, key, want=list):
    """First value under `key` that parses and matches `want`.

    The page mentions the key more than once — recommendations and other
    products carry their own blocks — so every occurrence is tried and the
    first that parses into the expected type wins. Returning the wrong block
    silently would price a line against another product's stock.
    """
    variants = [text]
    if "\\" in text:
        alt = _unescape(text)
        if alt != text:
            variants.append(alt)

    errors = 0
    for variant in variants:
        for start in _offsets(variant, key):
            try:
                raw = _scan(variant, start)
            except ExtractError:
                errors += 1
                continue
            try:
                val = json.loads(raw)
            except ValueError:
                errors += 1
                continue
            if isinstance(val, want) and val:
                return val
    raise ExtractError(
        f"no usable {key!r} block in the page ({errors} candidates failed to "
        f"parse). The page layout changed — refusing to guess a quantity.")


def availability(html):
    """The Onninen availability array, ready for OnninenAdapter.parse()."""
    return extract(html, "availability", want=list)
