"""Stable interface of the de-identification pipeline. Owned by the orchestrator: do not edit, ask instead.

A handler is a pure function  handler(data: bytes, ctx: Ctx) -> Result.
Same input bytes + same old path + same mapping version must always give the same output bytes.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional, List, Tuple

API_VERSION = 1


@dataclass
class Ctx:
    old_path: str                      # corpus-relative path; an archive member is "<archive path>!/<member path>"
    new_path: str                      # planned output path for this file
    source: str                        # first three components of the top-level file's path, e.g. sources/github/a__b
    mapping: object                    # deid.mapping.Mapping
    rename: Callable[[str], str]       # old corpus path, or bare old file name, -> new one (the path plan)
    recurse: Callable[[bytes, str], "Result"]   # transform an embedded file: (bytes, member old path) -> Result
    notes: List[str] = field(default_factory=list)


@dataclass
class Result:
    data: Optional[bytes]              # new bytes, or None meaning "unchanged, copy the input"
    changed: int = 0                   # replacements made
    status: str = "ok"                 # ok | unchanged | partial | failed | skipped
    notes: List[str] = field(default_factory=list)   # short human notes; they feed the run log and README.txt


REGISTRY: List[Tuple[int, str, Callable[[str, bytes], bool], Callable]] = []


def register(name: str, match: Callable[[str, bytes], bool], priority: int = 50):
    """Decorator. match(path_lower, first_4096_bytes) -> bool. Highest priority wins; name must be unique."""
    def deco(fn):
        REGISTRY.append((priority, name, match, fn))
        REGISTRY.sort(key=lambda t: (-t[0], t[1]))
        fn.handler_name = name
        return fn
    return deco


def pick(path: str, head: bytes):
    pl = path.lower()
    for _, name, match, fn in REGISTRY:
        try:
            if match(pl, head):
                return fn
        except Exception:
            continue
    return None
