"""Byte-level identity substitution with Aho-Corasick.

Original by a teammate, copied into pipeline_v2 on 2026-11-21 17:58 EST. Changes by the integrator, each marked V2:
  V2-1 one boundary rule for names on disk and names inside files ('slot' rule from for_andre/dropins.patch_engine);
       the `path` flag no longer changes which spans match, so rename(name) == sub_text(name) for every file name.
  V2-2 IGES Hollerith prefix ("12HJohn Doe") counts as a word boundary.
  V2-3 sub_readable(): same-length substitution only where the hit sits in readable text (council D9: no matcher over
       undecoded binary data). Used for binary kinds instead of a whole-file raw sweep.

Bytes are viewed as Latin-1 text (1 byte = 1 char) and lowercased, so one automaton matches identities
in UTF-8, Latin-1 and UTF-16LE encodings, case-insensitively, with exact byte offsets.
Every replacement is produced by pseudo.pseudo(), which preserves byte length.
"""
import re

import ahocorasick

from .pseudo import norm, pseudo

ENCODINGS = ('utf-8', 'utf-16le')
_ALNUM = re.compile(r'[0-9a-z]')   # ASCII only: high bytes next to a name are usually binary length/markers
PATH_SEP = set('/\\:"\'<>|\x00 \t\r\n')
_PRINT = frozenset(range(0x20, 0x7f)) | {9, 10, 13}
_LETTER = re.compile(rb'[A-Za-z\x80-\xff]')
_PAYLOAD = re.compile(rb'(?:base64,)?[A-Za-z0-9+/=\r\n]{120,}|[0-9A-Fa-f\r\n ]{128,}')


def _hollerith_before(low, s):
    """V2-2: True when low[:s] ends with <non-alnum or start><digits>'h' (IGES Hollerith string prefix)."""
    if s < 2 or low[s - 1] != 'h' or not low[s - 2].isdigit():
        return False
    j = s - 2
    while j >= 0 and low[j].isdigit():
        j -= 1
    return j < 0 or not _ALNUM.match(low[j])


class Engine:
    def __init__(self, identities):
        """identities: {norm_id: {'forms': [...], 'kind': str, 'mode': 'slot'|'word'|'sub'}}"""
        self.ids = identities
        A = ahocorasick.Automaton()
        n = 0
        for iid, rec in identities.items():
            forms = set(rec.get('forms', [])) | {iid}
            # separator variants for multi-word identities
            for f in list(forms):
                if ' ' in f:
                    for j in ('_', '-', '.', ''):
                        forms.add(f.replace(' ', j))
            for f in forms:
                for enc in ENCODINGS:
                    try:
                        b = f.encode(enc)
                    except UnicodeEncodeError:
                        continue
                    key = b.decode('latin-1').lower()
                    if len(f) < 2:
                        continue
                    A.add_word(key, (iid, rec['mode'], enc, len(key)))
                    n += 1
        A.make_automaton() if n else None
        self.A = A if n else None

    def _ok_boundary(self, low, s, e, enc, mode, path=False):
        # V2-1: `path` is accepted for compatibility and ignored: one rule for disk names and content.
        if mode == 'sub':
            return True
        w = 2 if enc == 'utf-16le' else 1
        if enc == 'utf-16le':
            b2 = low[s - 2:s] if s >= 2 else ''
            a2 = low[e:e + 2]
            before = b2[0] if len(b2) == 2 and b2[1] == '\x00' else ''
            after = a2[0] if len(a2) == 2 and a2[1] == '\x00' else ''
        else:
            before = low[s - 1] if s >= 1 else ''
            after = low[e] if e < len(low) else ''
        if mode == 'word':
            if before and _ALNUM.match(before) and not (w == 1 and _hollerith_before(low, s)):
                return False
            return not (after and _ALNUM.match(after))
        # 'slot' (very short identities): only as a whole path segment: C:\Users\jo\ , /home/jo/ , \\jo\share
        if before == '\\':
            return after == '' or after in PATH_SEP or after == '.'
        if before == '/':
            return after in ('', '/', '\\', '.')
        return before == '' and after in ('', '/', '\\', '.')

    def find(self, data, path=False):
        """Non-overlapping matches over bytes: list of (start, end, iid, enc)."""
        if not self.A or not data:
            return []
        low = data.decode('latin-1').lower()
        hits = []
        for end, (iid, mode, enc, L) in self.A.iter(low):
            s, e = end - L + 1, end + 1
            if self._ok_boundary(low, s, e, enc, mode, path):
                hits.append((s, e, iid, enc))
        hits.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        out, last = [], -1
        for h in hits:
            if h[0] >= last:
                out.append(h); last = h[1]
        return out

    def _apply(self, data, hits, same_len):
        if not hits:
            return data, 0
        parts, pos, n = [], 0, 0
        for s, e, iid, enc in hits:
            try:
                surf = data[s:e].decode(enc)
            except UnicodeDecodeError:
                continue
            rep = pseudo(surf, iid).encode(enc)
            if same_len and len(rep) != e - s:
                continue
            parts.append(data[pos:s]); parts.append(rep); pos = e; n += 1
        parts.append(data[pos:])
        return b''.join(parts), n

    def sub_bytes(self, data, same_len=True, path=False):
        return self._apply(data, self.find(data, path), same_len)

    def sub_text(self, text, path=False):
        # V2-4: lone surrogates (non-UTF-8 text decoded with surrogateescape) must pass through, not crash
        out, n = self.sub_bytes(text.encode('utf-8', 'surrogatepass'), same_len=False, path=path)
        return out.decode('utf-8', 'surrogatepass'), n

    # ------------------------------------------------------------------ V3-1 text CAD formats
    def find_cad(self, data):
        """Hits allowed in text CAD files: the hit holds a letter, neither neighbour is a digit (numbers are never
        touched), and it is not inside a base64 or hex payload (embedded images in SVG, XML, PostScript, HTML)."""
        hits = self.find(data)
        if not hits:
            return hits
        blobs = [(m.start(), m.end()) for m in _PAYLOAD.finditer(data)]
        out = []
        for h in hits:
            s, e = h[0], h[1]
            seg = data[s:e]
            if not _LETTER.search(seg):
                continue
            if (s > 0 and 48 <= data[s - 1] <= 57) or (e < len(data) and 48 <= data[e] <= 57):
                continue
            if any(a <= s < b for a, b in blobs):
                continue
            out.append(h)
        return out

    def sub_cad(self, data):
        return self._apply(data, self.find_cad(data), True)

    def sub_cad_text(self, text):
        b = text.encode('utf-8', 'surrogatepass')
        out, n = self._apply(b, self.find_cad(b), True)
        return out.decode('utf-8', 'surrogatepass'), n

    # ------------------------------------------------------------------ V2-3
    @staticmethod
    def _readable(data, s, e, enc):
        """A hit counts only inside readable text: names of 6+ characters need readable bytes of their own; shorter
        names need 6 more readable characters around them (chance hits in binary geometry are 4-5 letter words)."""
        w = 2 if enc == 'utf-16le' else 1
        nchar = (e - s) // w
        need = 0 if nchar >= 6 else 6

        def ok(i):
            if w == 1:
                return data[i] in _PRINT
            return i + 1 < len(data) and data[i] in _PRINT and data[i + 1] == 0
        if w == 1 and not all(b in _PRINT or b >= 0x80 for b in data[s:e]):
            return False
        if need == 0:
            return True
        got, i = 0, s - w
        while i >= 0 and got < need and ok(i):
            got += 1; i -= w
        i = e
        while i < len(data) and got < need and ok(i):
            got += 1; i += w
        return got >= need

    def sub_readable(self, data):
        hits = [h for h in self.find(data) if self._readable(data, h[0], h[1], h[3])]
        return self._apply(data, hits, True)
