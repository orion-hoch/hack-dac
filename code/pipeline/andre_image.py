"""the teammate's do_image (PNG text chunks), copied verbatim from his process.py so v2 does not import the legacy PDF path."""
import struct
import zlib


def do_image(engine, data):
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        out, pos, n = [data[:8]], 8, 0
        while pos + 8 <= len(data):
            L = struct.unpack('>I', data[pos:pos + 4])[0]
            typ = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + L]
            if typ in (b'tEXt', b'iTXt', b'eXIf'):
                body, k = engine.sub_bytes(body, same_len=False); n += k
            elif typ == b'zTXt':
                try:
                    kw, rest = body.split(b'\x00', 1)
                    txt = zlib.decompress(rest[1:])
                    txt, k = engine.sub_bytes(txt, same_len=False); n += k
                    body = kw + b'\x00' + rest[:1] + zlib.compress(txt)
                except Exception:
                    pass
            out.append(struct.pack('>I', len(body)) + typ + body + struct.pack('>I', zlib.crc32(typ + body) & 0xffffffff))
            pos += 12 + L
            if typ == b'IEND':
                break
        out.append(data[pos:])
        return b''.join(out), n
    return engine.sub_bytes(data, same_len=True)
