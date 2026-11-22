"""Deterministic, format-preserving pseudonyms.

Every character keeps its class and byte width: vowel->vowel, consonant->consonant, digit->digit,
accented letter->accented letter (same combining mark), CJK->CJK, Cyrillic->Cyrillic, separators kept.
So a replacement is the same length in UTF-8, Latin-1 and UTF-16, which keeps binary formats valid
(OLE property sizes, IGES Hollerith counts, STL headers, ACIS strings).

The letters are chosen per *normalized identity* (accent-folded, lowercase) and per position, so every
surface form of one identity ("Andrés", "ANDRES", "andres") maps to the same pseudonym up to case/accents.
"""
import hashlib
import re
import unicodedata

VOW = 'aeiou'
CON = 'bcdfghjklmnprstvz'
CYR_VOW = 'аеиоуя'
CYR_CON = 'бвгдклмнпрстфх'
CJK_POOL = '安白陈德方高韩何黄江金康李林刘罗马孟宁潘钱秦任宋孙唐田王吴肖谢徐许杨叶于张赵郑周朱文明华建国平志海军伟强磊洋勇艳杰涛超秀英'


def norm(s):
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.lower().split())


def _stream(key):
    """Deterministic byte stream from the identity key."""
    i = 0
    while True:
        for b in hashlib.sha256(f'{key}|{i}'.encode('utf-8')).digest():
            yield b
        i += 1


def _fold(ch):
    d = unicodedata.normalize('NFD', ch)
    base = ''.join(c for c in d if not unicodedata.combining(c))
    marks = ''.join(c for c in d if unicodedata.combining(c))
    return base, marks


TOKEN = re.compile(r'[^\W_]+')
CAMEL = re.compile(r'(?<=[a-zà-ÿ])(?=[A-ZÀ-Þ])')


def _token(tok):
    """Pseudonym for one alphanumeric token, seeded by the token itself (accent/case-folded)."""
    rnd = _stream(norm(tok))
    out = []
    for ch in tok:
        r = next(rnd)
        base, marks = _fold(ch)
        lb = base.lower()
        if len(lb) == 1 and 'a' <= lb <= 'z':
            pool = VOW if lb in VOW else CON
            nb = pool[r % len(pool)]
            if nb == lb:
                nb = pool[(r + 1) % len(pool)]
            if base.isupper():
                nb = nb.upper()
            new = unicodedata.normalize('NFC', nb + marks) if marks else nb
            if len(new.encode('utf-8')) != len(ch.encode('utf-8')):
                new = ch if marks else nb
            out.append(new)
        elif ch.isdigit() and ch.isascii():
            nd = str(r % 10)
            if nd == ch:
                nd = str((r + 1) % 10)
            out.append(nd)
        elif '\u4e00' <= ch <= '\u9fff':
            out.append(CJK_POOL[r % len(CJK_POOL)])
        elif '\u0430' <= ch.lower() <= '\u044f':
            pool = CYR_VOW if ch.lower() in CYR_VOW else CYR_CON
            nc = pool[r % len(pool)]
            out.append(nc.upper() if ch.isupper() else nc)
        else:
            out.append(ch)
    return ''.join(out)


def pseudo(surface, key=None):
    """Pseudonym for a surface form. Each word (split on separators and camelCase) is seeded by itself, so
    'Alex Morgan', 'morgan', 'AlexMorgan' and 'alex_morgan' all share the same pseudonymous words."""
    def tok(m):
        parts = CAMEL.split(m.group())
        return ''.join(_token(p) for p in parts)
    res = TOKEN.sub(tok, surface)
    if norm(res) == norm(surface):          # never return the original
        res = TOKEN.sub(lambda m: _token(m.group() + 'x')[:len(m.group())], surface)
    return res
