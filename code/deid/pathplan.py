"""Path plan: old corpus path -> new corpus path. CORE builder.

Policy (D2 default, measured reason in findings/verifier.md: renaming file names makes 93% of files unfindable):
  * component 3 (the source folder: owner__repo, owner__repo__<hash>, a web domain, a collection title) is replaced;
    "owner__repo" and "owner__repo__hash" are split on "__" so both give the same owner and repo stand-ins;
  * deeper folder and file names keep every byte except identity words: words the mapping knows, plus the source's
    own owner and repo words (case-insensitive, whole words);
  * the extension chain (".step", ".tar.gz", ".drw.1", ".extracted") is never touched, so "<x>.extracted/" follows
    the new name of its archive "<x>.zip" automatically;
  * components that do not change are returned as the very same str object (no Unicode normalisation).
Stand-ins are same length, so a renamed name can be patched in place inside file content.
"""
import re, unicodedata

TOP_KINDS = ("github", "gitlab", "existing", "catalog", "web")
_EXT = re.compile(r"\.[A-Za-z0-9_]{1,10}$")
# extension tokens that may sit in front of the last one ("x.stp.tar.gz", "x.drw.1", "x.sat.wrl")
_CHAIN_OK = {"tar", "gz", "z", "zip", "stp", "step", "sat", "wrl", "drw", "prt", "asm", "frm", "hh", "smdg", "delta",
             "unv", "pff", "sprt", "fea", "pmd", "mf1", "mf2", "mdb", "pdb", "prg", "igs", "iges", "x_t", "dxf", "stl",
             "extracted", "pdf", "txt", "brp", "fcstd", "f3d", "sldprt", "sldasm", "slddrw"}
_HEX = re.compile(r"^[0-9a-f]{6,40}$")


def split_ext(name):
    """('stem', '.ext.chain'). The last extension always counts; earlier ones only if known or numeric."""
    stem, ext = name, ""
    m = _EXT.search(stem)
    if not m or m.start() == 0:
        return name, ""
    ext = m.group(0); stem = stem[:m.start()]
    while True:
        m = _EXT.search(stem)
        if not m or m.start() == 0:
            break
        tok = m.group(0)[1:].lower()
        if tok in _CHAIN_OK or tok.isdigit():
            ext = m.group(0) + ext; stem = stem[:m.start()]
        else:
            break
    return stem, ext


class PathPlan:
    def __init__(self, mapping):
        self.mapping = mapping
        self._memo = {}
        self._src_words = {}

    # ---- source folder -------------------------------------------------------------------------------------
    def _source_parts(self, parts):
        """Index of the source-folder component and its kind, or (None, None)."""
        if len(parts) >= 3 and parts[0] == "sources" and parts[1] in TOP_KINDS:
            return 2, parts[1]
        if len(parts) >= 2 and parts[0] == "archives":
            return 1, "archives"
        return None, None

    def source_folder(self, comp, kind):
        """New name of a source folder. owner__repo[__hash] is split so both forms agree."""
        key = ("S", comp)
        if key in self._memo:
            return self._memo[key]
        if kind == "archives":                      # archives/owner__repo__hash.zip
            stem, ext = split_ext(comp)
            new = self.source_folder(stem, "github") + ext
        elif kind in ("github", "gitlab") and "__" in comp:      # council D2: hash tokens stay verbatim
            new = "__".join(p if (not p or _HEX.match(p)) else self.mapping.sub_path_component(p, 2) for p in comp.split("__"))
        else:
            new = self.mapping.sub_path_component(comp, 2)
        if len(new) != len(comp):
            raise ValueError("mapping returned a stand-in of different length for a source folder")
        self._memo[key] = new
        return new

    def source_words(self, comp, kind):
        """Disabled by council D2: one global tokenizer for paths and content, no per-source rules. Kept as a stub."""
        return None
        if comp in self._src_words:
            return self._src_words[comp]
        words = []
        base = split_ext(comp)[0] if kind == "archives" else comp
        if kind in ("github", "gitlab", "archives") and "__" in base:
            words = [p for p in base.split("__")[:2] if len(p) >= 4 and not _HEX.match(p)]
        elif kind in ("web",):
            words = [p for p in re.split(r"[.]", base) if len(p) >= 4 and p.lower() not in ("www", "com", "org", "net", "html")]
        elif kind == "catalog":
            words = []                             # collection titles are long sentences: whole-title match only
        rx = None
        if words:
            rx = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                            + r")(?![A-Za-z0-9])", re.I)
        self._src_words[comp] = rx
        return rx

    # ---- deeper components ---------------------------------------------------------------------------------
    def component(self, comp, depth, src_rx=None, source=None):
        key = (comp, depth > 2, src_rx.pattern if src_rx else None)
        if key in self._memo:
            return self._memo[key]
        stem, ext = split_ext(comp)
        new = stem
        if src_rx is not None:
            new = src_rx.sub(lambda m: self.mapping.standin(m.group(0)), new)
        new = self.mapping.sub_path_component(new, max(depth, 3), source)
        if len(new) != len(stem):
            new = stem                              # never accept a length change in a deep component
        out = comp if new == stem else new + ext
        self._memo[key] = out
        return out

    def plan(self, old):
        """Full corpus-relative path (str) -> new path (str). Archive members use '<archive>!/<member>'."""
        if "!/" in old:
            outer, inner = old.split("!/", 1)
            return self.plan(outer) + "!/" + self.member(inner, outer)
        parts = old.split("/")
        i, kind = self._source_parts(parts)
        if i is None:
            return "/".join(self.component(p, 9) for p in parts)
        src_comp = parts[i]
        rx = self.source_words(src_comp, kind)
        source = "/".join(parts[:i + 1])
        if kind == "existing":                     # council 2026-11-21: drawing-search etc. are generic, never renamed;
            out = parts[:i] + [self.component(src_comp, 3, None, source)]   # identities sit deeper, token level
        else:
            out = parts[:i] + [self.source_folder(src_comp, kind)]
        for d, p in enumerate(parts[i + 1:], start=i + 1):
            out.append(self.component(p, d, rx, source))
        return "/".join(out)

    def member(self, inner, outer_old):
        """Member path inside an archive: deep rule, with the outer file's source words."""
        parts = outer_old.split("!/")[0].split("/")
        i, kind = self._source_parts(parts)
        rx = self.source_words(parts[i], kind) if i is not None else None
        source = "/".join(parts[:i + 1]) if i is not None else None
        return "/".join(self.component(p, 9, rx, source) if p else p for p in inner.split("/"))

    def rename(self, old, context_path=None):
        """ctx.rename: a full corpus path -> plan(); a bare name or a foreign path -> deep rule per component."""
        if old.startswith(("sources/", "archives/")):
            return self.plan(old)
        sep = "\\" if ("\\" in old and "/" not in old) else "/"
        rx = None; source = None
        if context_path:
            parts = context_path.split("!/")[0].split("/")
            i, kind = self._source_parts(parts)
            if i is not None:
                rx = self.source_words(parts[i], kind); source = "/".join(parts[:i + 1])
        return sep.join(self.component(p, 9, rx, source) if p else p for p in old.split(sep))


def check_collisions(pairs):
    """pairs: iterable of (old, new). Returns list of (kind, new, [olds]) for exact and case/NFC-insensitive clashes."""
    seen, folded, out = {}, {}, []
    for old, new in pairs:
        if new in seen and seen[new] != old:
            out.append(("exact", new, [seen[new], old]))
        seen.setdefault(new, old)
        f = unicodedata.normalize("NFC", new).casefold()
        fo = unicodedata.normalize("NFC", old).casefold()
        if f in folded and folded[f][1] != fo:      # clash created by the plan, not present in the input already
            out.append(("casefold", new, [folded[f][0], old]))
        folded.setdefault(f, (old, fo))
    return out
