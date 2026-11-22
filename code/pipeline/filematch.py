"""Per-file identities (council D9). Team module: sits next to pseudo.py and engine.py (same package).

A handler passes the identity-field values it found in THIS file (author, last saved by, drawn by, checked by,
including initials of two or three letters). FileValues finds those values inside the same file's decoded text
slots, in UTF-8 / Latin-1 and UTF-16LE, as whole tokens only:
  * never right after a format sigil or joiner  / \\ # $ @ & % = _ . - ~  (so /DS, #12=XQZ, x.QZW, QZW_part stay);
  * never right before a letter, digit, _ - = ( ~ ;
  * values of 3 characters or fewer: exact case or all upper case, letters (and dots) only; 4 or more: any case.
The stand-in is pseudo(surface), so a value gets the same stand-in here as in its field and everywhere else.
"""
import re

from .pseudo import norm

_BAD_BEFORE = rb"/\\#$@&%=_.\-~"
_BAD_AFTER = rb"_\-=(~"


def _ok(v, stop):
    n = norm(v)
    if len(n) < 2 or n in stop or not re.search(r"[^\W\d_]", v):
        return False
    if len(n) <= 3 and not re.fullmatch(r"[^\W\d_](?:\.?[^\W\d_]){1,2}\.?", v):
        return False
    return True


class FileValues:
    def __init__(self, values, stop=()):
        vals = sorted({v.strip(" \x00\t\r\n:;,") for v in values if v and v.strip()}, key=lambda x: (-len(x), x))
        self.values = [v for v in vals if _ok(v, stop)]
        alts8, alts16 = [], []
        for v in self.values:
            forms = {v, v.upper()}
            for f in sorted(forms, key=lambda x: (-len(x), x)):
                b8, b16 = f.encode("utf-8"), f.encode("utf-16-le")
                ci = len(norm(v)) >= 4
                alts8.append((b"(?i:%s)" if ci else b"%s") % re.escape(b8))
                alts16.append((b"(?i:%s)" if ci else b"%s") % re.escape(b16))
        self._rx8 = re.compile(rb"(?<![A-Za-z0-9\x80-\xff" + _BAD_BEFORE + rb"])(?:" + b"|".join(alts8) +
                               rb")(?![A-Za-z0-9\x80-\xff" + _BAD_AFTER + rb"])") if alts8 else None
        self._rx16 = re.compile(rb"(?<![A-Za-z0-9" + _BAD_BEFORE + rb"]\x00)(?:" + b"|".join(alts16) +
                                rb")(?![A-Za-z0-9" + _BAD_AFTER + rb"]\x00)") if alts16 else None

    def find(self, data):
        """[(start, end, encoding)] of every whole-token occurrence (may overlap global hits; caller resolves)."""
        out = []
        if self._rx8:
            out += [(m.start(), m.end(), "utf-8") for m in self._rx8.finditer(data)]
        if self._rx16:
            out += [(m.start(), m.end(), "utf-16le") for m in self._rx16.finditer(data)]
        return out
