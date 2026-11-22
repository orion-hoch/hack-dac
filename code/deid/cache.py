"""Content cache. CORE builder.

Key = sha256(input bytes) + handler name + handler version + mapping version + old basename + new basename.
The basename is in the key because content often holds its own file name (STEP FILE_NAME, PRODUCT, IGES G4).
The folder and the source are NOT in the key on purpose: identical bytes under the same name in two forks must come
out identical (consistency across owners). A handler that scopes by ctx.source therefore gives the first source's
result to later copies; that is the intended trade-off and is listed in findings/build_core.md.
Entries are written atomically (tmp + rename) so parallel workers and restarts never see half a file.
"""
import hashlib, json, os


def key(data_sha, handler, hversion, mversion, old_name, new_name):
    s = "\x1f".join([data_sha, handler, str(hversion), str(mversion), old_name, new_name])
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


class Cache:
    def __init__(self, root):
        self.root = root
        self.mem = {}                       # per-process memo for archive members seen twice in one run
        if root:
            os.makedirs(root, exist_ok=True)

    def _p(self, k):
        return os.path.join(self.root, k[:2], k)

    def get(self, k):
        if k in self.mem:
            return self.mem[k]
        if not self.root:
            return None
        p = self._p(k)
        try:
            meta = json.load(open(p + ".json", encoding="utf-8"))
        except (OSError, ValueError):
            return None
        data = None
        if meta.get("has_data"):
            try:
                data = open(p + ".bin", "rb").read()
            except OSError:
                return None
        return data, meta

    def put(self, k, data, meta, keep_mem=False):
        meta = dict(meta, has_data=data is not None)
        if keep_mem and (data is None or len(data) < (8 << 20)):
            self.mem[k] = (data, meta)
        if not self.root:
            return
        p = self._p(k)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = "%s.%d.tmp" % (p, os.getpid())
        if data is not None:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p + ".bin")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, p + ".json")
