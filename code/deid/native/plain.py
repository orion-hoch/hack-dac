"""Call a handler as a plain function (bytes in, bytes out), for pipelines that do not use deid.api. Owner: NATIVE."""
from deid.api import Ctx, Result

_M = None


def _mapping():
    global _M
    if _M is None:
        import sys
        from deid.mapping import Mapping
        _M = Mapping()
        if str(getattr(_M, "version", "")).endswith("tnone"):
            print("deid.native.plain: WARNING the identity table is EMPTY (mapping version %s)" % _M.version,
                  file=sys.stderr)
    return _M


def call(handle, data, old_path, mapping=None, source=None, rename=None, new_path=None):
    """Run handle(data, ctx) with a minimal Ctx. Returns the Result."""
    ctx = Ctx(old_path=old_path, new_path=new_path or old_path,
              source=source or "/".join(old_path.split("/")[:3]),
              mapping=mapping or _mapping(), rename=rename or (lambda s: s),
              recurse=lambda b, p: Result(None, status="unchanged"))
    return handle(data, ctx)


def make_transform(handle):
    def transform(data, old_path, mapping=None, source=None, rename=None):
        """bytes in, bytes out; the input itself when nothing changed or the handler failed."""
        r = call(handle, data, old_path, mapping, source, rename)
        return r.data if r.data is not None else data
    return transform
