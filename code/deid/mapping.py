"""Name mapping. The API below is stable (stub-1 callers keep working); the mapping builder owns the internals and data/.

The team uses ONE stand-in function (council D1: one global keyed letter map, data/vendor/andre_pipeline/pseudo.py,
identical to ~/hack-osu/for_andre/mapping/pseudo.py), and the teammate's Aho-Corasick Engine (engine.py, verbatim)
decides what is matched. Both are pinned by SHA-256 below; this module refuses to load a copy that differs. Our part is the identity tables in data/ (his identities.json format,
plus a "scope" field his engine ignores).

Guarantees every handler may rely on (measured in tests/test_mapping.py):
  * len(standin(x)) == len(x) in code points, UTF-8 and UTF-16 bytes; letters stay letters (case kept), digits digits.
  * deterministic from the secret key; standin(x.upper()) == standin(x).upper() for ASCII; standin(a + b) ==
    standin(a) + standin(b) between graphemes; composed and decomposed spellings agree; a bijection (no collisions).
"""
import functools, hashlib, json, os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(_HERE, "..", "data"))          # ships: code, vendored modules, generic stop list
# Tables that contain original identifiers are private run inputs and never ship with the code (council, 2026-11-21).
PRIVATE = os.environ.get("DEID_PRIVATE_DATA") or os.path.normpath(os.path.join(_HERE, "..", "private_data"))
_VENDOR = os.path.join(DATA, "vendor")
PIN = {"pseudo.py": "a9959d36478ffffc1c1f0a666e874eda11028698c347b2a59be8a2e45a073a0b",
       "engine.py": "32997c308d14e76638020c8ccc5dac0012edf5f3da1613d44533449df8b38ac4"}


def _sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


for _fn, _h in PIN.items():
    if _sha(os.path.join(_VENDOR, "andre_pipeline", _fn)) != _h:
        raise RuntimeError("data/vendor/andre_pipeline/%s differs from the pinned copy; re-pin deliberately" % _fn)
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
from andre_pipeline import pseudo as _P, engine as _E   # noqa: E402

# Council D1: ONE GLOBAL KEYED LETTER MAP (data/vendor/andre_pipeline/pseudo.py is the team function, the same file as
# ~/hack-osu/for_andre/mapping/pseudo.py). The key is secret: $DEID_PSEUDO_KEY or the file $DEID_PSEUDO_KEY_FILE
# (default ~/hack-osu/private/pseudo.key); without one the public DEV key is used and a warning printed.
MAPPING_VERSION = "global-%s-%s-key-%s" % (PIN["pseudo.py"][:8], PIN["engine.py"][:8], _P.key_id())
norm = _P.norm


def standin(s: str) -> str:
    return _P.pseudo(s)


def source_of(path: str) -> str:
    q = path.split("!/")[0].split("/")
    return "/".join(q[:3]) if q and q[0] == "sources" else "/".join(q[:2])


class Mapping:
    """Identity tables from $DEID_PRIVATE_DATA/identities.json: {norm_id: {"forms", "kind", "mode", "scope", ...}}.
    scope: "global" (matched everywhere), "field" (only via sub_field / is_identity(in_field=True)), or a list of
    sources (ctx.source strings) where it is matched in text and paths; an entry "path:<source>" means: only in the
    source folder name itself (ordinary words such as Hardware). sources/existing/<x> folder names are never renamed."""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir or PRIVATE
        p = os.path.join(self.data_dir, "identities.json")
        self.ids = {}
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                self.ids = json.load(f)
        self.stop = set()
        sp = os.path.join(DATA, "stoplist.tsv")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                for line in f:
                    a = line.rstrip("\n").split("\t")
                    if a[0] and not a[0].startswith("#"):
                        self.stop.add(norm(a[0]))
        self.version = MAPPING_VERSION + "-t" + (_sha(p)[:8] if os.path.exists(p) else "none")
        self.glob, self.scoped, self.pathonly = {}, {}, {}
        for iid, r in self.ids.items():
            sc = r.get("scope", "global")
            if sc == "global":
                self.glob[iid] = r
            elif isinstance(sc, list):
                for s in sc:
                    if s.startswith("path:"):
                        self.pathonly.setdefault(s[5:], {})[iid] = r
                    else:
                        self.scoped.setdefault(s, {})[iid] = r
        self._g = None
        self._eng = functools.lru_cache(maxsize=64)(self._build)

    def _build(self, source, folder=False):
        recs = self.scoped.get(source) if source else None
        if folder and source in self.pathonly:
            return _E.Engine({**self.glob, **(recs or {}), **self.pathonly[source]})
        if not recs:
            if self._g is None:
                self._g = _E.Engine(self.glob)
            return self._g
        return _E.Engine({**self.glob, **recs})

    def engine(self, source=None):
        return self._eng(source if source in self.scoped else None)

    def standin(self, value: str) -> str:
        return standin(value)

    def is_identity(self, value: str, source: str = None, in_field: bool = False) -> bool:
        r = self.ids.get(norm(value))
        if not r:
            return False
        sc = r.get("scope", "global")
        return sc == "global" or in_field or (isinstance(sc, list) and source in sc)

    def sub_field(self, value: str, source: str = None) -> str:
        """Value of a field that is an identity by definition (author, last saved by). Same length.
        Generic values on the stop list (Administrator, Unknown, Author ...) stay."""
        if not value.strip() or norm(value.strip(" \x00")) in self.stop:
            return value
        return standin(value)

    def sub_text(self, text: str, source: str = None):
        """Replace known identities in free text. Returns (new text, count). Same length."""
        return self.engine(source).sub_text(text)

    def sub_bytes(self, blob: bytes, source: str = None, encodings=("utf-8", "utf-16-le")):
        """Replace known identities inside decoded-but-binary text (Latin-1/UTF-8 and UTF-16LE views at once).
        Returns (new bytes, count). Same length. `encodings` is kept for stub-1 callers; the engine fixes them."""
        return self.engine(source).sub_bytes(blob, same_len=True)

    def sub_path_component(self, component: str, depth: int, source: str = None) -> str:
        """One path component -> its new name (identity words only; the rest, and the extension, stay)."""
        if depth == 2 and source and source.startswith("sources/existing/"):
            return component
        if depth == 2 and source in self.pathonly:
            return self._eng(source, True).sub_text(component, path=True)[0]
        return self.engine(source).sub_text(component, path=True)[0]
