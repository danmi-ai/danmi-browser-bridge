"""Tiny semver-ish version comparator (no pre-release support).

Used by #15 / #16 to enforce ``min_version`` on WS reconnect, and by the
extension to compare local version vs server-published version (#8B).
"""

from __future__ import annotations


def _parts(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints. Missing/non-numeric
    components are treated as 0. ``"0.4.0"`` -> ``(0, 4, 0)``.
    """
    if not v:
        return (0,)
    out: list[int] = []
    for piece in str(v).split("."):
        try:
            out.append(int(piece))
        except ValueError:
            # Strip any "-rc1" / "+build" suffix and try again, else 0.
            num = ""
            for ch in piece:
                if ch.isdigit():
                    num += ch
                else:
                    break
            out.append(int(num) if num else 0)
    return tuple(out) or (0,)


def compare(a: str, b: str) -> int:
    """Return -1 if a<b, 0 if equal, 1 if a>b."""
    pa, pb = _parts(a), _parts(b)
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


def lt(a: str, b: str) -> bool:
    return compare(a, b) < 0
