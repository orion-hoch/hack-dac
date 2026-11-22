"""Fallback for a compressed block that no longer fits: shorter stand-ins padded with spaces. Owner: NATIVE builder.
Every run of changed bytes keeps its first 3 changed bytes and the rest become spaces (0x20). In UTF-16 text only the
low bytes differ, so the high bytes stay 0 and the result is still valid UTF-16. Lengths never change."""


def squeeze(orig, new, keep=3):
    diff = [i for i in range(len(new)) if new[i] != orig[i]]
    if not diff:
        return new
    b = bytearray(new)
    run, last = 0, -10
    for i in diff:
        run = run + 1 if i - last <= 2 else 1
        if run > keep:
            b[i] = 0x20
        last = i
    return bytes(b)
