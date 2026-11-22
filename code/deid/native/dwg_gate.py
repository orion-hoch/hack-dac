"""Strict gate for DWG edits (council, leg 3). Owner: NATIVE builder.

The independent reader LibreDWG must see the same drawing in the original and the output except for string values:
same JSON tree (same keys, same list lengths, hence the same entity and object counts), every differing leaf a
string of the same length whose changed characters keep their class (letter for letter, digit for digit), not a
hex dump, and not under a key that holds raw data (unknown_bits, acis_data, data, ...; "xdata" is text and allowed).
Edits inside objects LibreDWG cannot parse (unknown_bits) cannot be confirmed and are therefore refused. Anything else: the edit is refused and the original goes out.
LibreDWG is an external CHECKER, not part of the transform (it is GPL; it is only run, never linked or copied).
Without the binary the DWG handler leaves content unchanged. Input-only limit: drawings over MAX_BYTES are not gated
(their JSON does not fit the memory budget), so their content is left unchanged.
"""
import json
import os
import re
import subprocess
import tempfile

DWGREAD = os.environ.get("DEID_DWGREAD") or os.path.expanduser("~/hack-osu/work/verifier/env/libredwg/bin/dwgread")
MAX_BYTES = 8_000_000
RAW_KEYS = {"data", "bindata", "unknown_bits", "acis_data", "sab", "blob", "bytes", "raw"}
_HEX = re.compile(r"[0-9A-Fa-f]{16,}")


def _raw_key(path):
    """True if the leaf sits under a key that holds raw data rendered as text (LibreDWG's hex dumps)."""
    for part in path.split("/"):
        base = re.sub(r"\[\d+\]", "", part).lower()
        if base in RAW_KEYS or base.endswith("_bits"):
            return True
    return False


def available():
    return os.path.isfile(DWGREAD) and os.access(DWGREAD, os.X_OK)


def read_json(data):
    """(parsed JSON or None, return code) of `dwgread -O JSON` on these bytes."""
    with tempfile.TemporaryDirectory(dir=os.environ.get("DEID_TMP")) as td:
        p, q = os.path.join(td, "in.dwg"), os.path.join(td, "out.json")
        with open(p, "wb") as fh:
            fh.write(data)
        r = subprocess.run([DWGREAD, "-O", "JSON", "-o", q, p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.exists(q):
            return None, r.returncode
        try:
            with open(q, encoding="utf-8", errors="replace") as fh:
                return json.load(fh), r.returncode
        except ValueError:
            return None, r.returncode


def _classes_ok(a, b):
    for x, y in zip(a, b):
        if x != y and not ((x.isalpha() and y.isalpha()) or (x.isdigit() and y.isdigit())):
            return False
    return True


def compare(ja, jb, limit=20):
    """(number of string leaves that differ, [non-string differences])."""
    bad, nstr = [], 0
    stack = [(ja, jb, "")]
    while stack and len(bad) < limit:
        a, b, path = stack.pop()
        if type(a) is not type(b):
            bad.append((path, "type", repr(a)[:40], repr(b)[:40]))
        elif isinstance(a, dict):
            if list(a.keys()) != list(b.keys()):
                bad.append((path, "keys", "", ""))
            else:
                stack.extend((a[k], b[k], path + "/" + k) for k in a)
        elif isinstance(a, list):
            if len(a) != len(b):
                bad.append((path, "length", len(a), len(b)))
            else:
                stack.extend((x, y, "%s[%d]" % (path, i)) for i, (x, y) in enumerate(zip(a, b)))
        elif a != b:
            if isinstance(a, str) and len(a) == len(b) and _classes_ok(a, b) and not _raw_key(path) \
                    and not _HEX.fullmatch(a):
                nstr += 1
            else:
                bad.append((path, "value", repr(a)[:40], repr(b)[:40]))
    return nstr, bad


def gate(orig, out):
    """(True, note) when the edit may go out; (False, reason) otherwise."""
    if not available():
        return False, "LibreDWG checker absent"
    if len(orig) > MAX_BYTES:
        return False, "drawing over %d bytes is not gated" % MAX_BYTES
    ja, ra = read_json(orig)
    jb, rb = read_json(out)
    if ja is None or jb is None:
        return False, "LibreDWG could not read %s" % ("the original" if ja is None else "the output")
    if ra != rb:
        return False, "LibreDWG return code %s -> %s" % (ra, rb)
    nstr, bad = compare(ja, jb)
    if bad:
        return False, "non-string change at %s (%s -> %s)" % (bad[0][0][-80:], bad[0][2], bad[0][3])
    return True, "LibreDWG: %d string values changed, nothing else" % nstr
