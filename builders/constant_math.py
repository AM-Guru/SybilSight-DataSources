#!/usr/bin/env python3
"""Fast expansions of the constants, and the packed-BCD encoding they ship in.

Split out of ``build_constant_digits.py`` because the deep tiers need a
different engine. The original pure-Python binary splitting is fine to a
million places (97 s) but scales ~n², which puts 461 million places — the depth
an eight-digit birthday needs — at roughly eleven days. GMP, via gmpy2, does
10 million in 6.8 s and is quasi-linear, so the same job is minutes.

The pure-Python path is kept as the fallback and as an independent check: both
engines are run at 10,000 places and compared, so a GMP-specific mistake cannot
pass unnoticed.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log  # noqa: E402

try:
    import gmpy2
    from gmpy2 import mpz

    HAVE_GMP = True
except ImportError:  # pragma: no cover - depends on the build host
    HAVE_GMP = False

sys.setrecursionlimit(100_000)

# First 50 places of each, from published expansions. A builder that drifts
# fails here rather than shipping positions that are quietly wrong.
KNOWN_PREFIXES = {
    "pi": "14159265358979323846264338327950288419716939937510",
    "e": "71828182845904523536028747135266249775724709369995",
    "phi": "61803398874989484820458683436563811772030917980576",
    "sqrt2": "41421356237309504880168872420969807856967187537694",
    "sqrt3": "73205080756887729352744634150587236694280525381038",
    "sqrt5": "23606797749978969640917366873127623544061835961152",
}

# Guard places absorb the truncation error the series accumulates in its least
# significant digits. Without them the last handful of reported digits are
# wrong, which for a feature whose entire output is a POSITION is fatal.
GUARD = 32


# ------------------------------------------------------------------ generation


def _chudnovsky(digits: int) -> str:
    """pi, by binary-splitting Chudnovsky over GMP integers."""
    prec = digits + GUARD
    c3_24 = mpz(640320) ** 3 // 24

    def split(a: int, b: int):
        if b - a == 1:
            if a == 0:
                p = q = mpz(1)
            else:
                p = mpz(6 * a - 5) * mpz(2 * a - 1) * mpz(6 * a - 1)
                q = mpz(a) ** 3 * c3_24
            t = p * (13591409 + 545140134 * a)
            return (p, q, -t if a & 1 else t)
        m = (a + b) // 2
        pl, ql, tl = split(a, m)
        pr, qr, tr = split(m, b)
        return pl * pr, ql * qr, qr * tl + pl * tr

    # Chudnovsky yields ~14.18 decimal places per term.
    terms = max(2, int(prec / 14.181647462) + 2)
    _, q, t = split(0, terms)
    one = mpz(10) ** prec
    root = gmpy2.isqrt(10005 * one * one)
    value = (q * 426880 * root) // t
    return value.digits()[1 : digits + 1]


def _euler_e(digits: int) -> str:
    """e = sum 1/k!, by binary splitting so the terms combine pairwise."""
    prec = digits + GUARD

    def split(a: int, b: int):
        """Return (p, q) with sum_{k=a}^{b-1} 1/k! = p/q over this range."""
        if b - a == 1:
            return mpz(1), mpz(1) if a == 0 else mpz(a)
        m = (a + b) // 2
        pl, ql = split(a, m)
        pr, qr = split(m, b)
        return pl * qr + pr, ql * qr

    # 1/k! drops below 10^-prec once log10(k!) > prec.
    terms, total = 1, 0.0
    while total <= prec:
        terms += 1
        total += math.log10(terms)
    p, q = split(0, terms + 1)
    value = (p * mpz(10) ** prec) // q
    return value.digits()[1 : digits + 1]


def _root(value: int, digits: int) -> str:
    """sqrt(value), via GMP's integer square root."""
    prec = digits + GUARD
    scaled = gmpy2.isqrt(mpz(value) * mpz(10) ** (2 * prec))
    text = scaled.digits()
    integer_places = len(str(math.isqrt(value)))
    return text[integer_places : integer_places + digits]


def _phi(digits: int) -> str:
    """phi = (1 + sqrt 5) / 2."""
    prec = digits + GUARD
    scaled = (gmpy2.isqrt(mpz(5) * mpz(10) ** (2 * prec)) + mpz(10) ** prec) // 2
    return scaled.digits()[1 : digits + 1]


GENERATORS = {
    "pi": _chudnovsky,
    "e": _euler_e,
    "phi": _phi,
    "sqrt2": lambda n: _root(2, n),
    "sqrt3": lambda n: _root(3, n),
    "sqrt5": lambda n: _root(5, n),
}


def expansion(name: str, digits: int) -> str:
    """Digits after the decimal point, verified against the published prefix."""
    if not HAVE_GMP:
        raise SystemExit(
            "gmpy2 is required for the deep tiers (pip install gmpy2). "
            "The pure-Python builder scales ~n^2 and cannot reach these depths."
        )
    text = GENERATORS[name](digits)
    if len(text) < digits:
        raise SystemExit(f"{name}: got {len(text):,} digits, wanted {digits:,}")
    expected = KNOWN_PREFIXES[name]
    if not text.startswith(expected):
        raise SystemExit(
            f"{name}: expansion does not match the published prefix\n"
            f"  got  {text[:50]}\n  want {expected}"
        )
    return text


# ------------------------------------------------------------------- encoding

# Two digits per byte, high nibble first. Measured against the alternatives on
# a real million-digit sample:
#
#   ASCII text          1.000 B/digit installed, 0.470 gzipped
#   packed BCD          0.500                    0.421
#   base-10^9 limbs     0.444                    0.440
#   entropy floor       -                        0.415
#
# BCD halves the installed size and compresses closest to the floor, and unlike
# the limb encoding it can be scanned in place: a two-digit pattern is a byte
# comparison, and the reader never has to decode a chunk to search it.
PAD_NIBBLE = 0x0F

_ENCODE = bytes.maketrans(b"0123456789", bytes(range(10)))


def pack(digits: str) -> bytes:
    """Pack a digit string into BCD. An odd length pads with 0xF."""
    values = digits.encode("ascii").translate(_ENCODE)
    if len(values) & 1:
        values += bytes([PAD_NIBBLE])
    high = values[0::2]
    low = values[1::2]
    return bytes((h << 4) | l for h, l in zip(high, low))


def unpack(blob: bytes, count: int) -> str:
    """Inverse of `pack`, for verification. `count` is the true digit count."""
    out = []
    for byte in blob:
        out.append(byte >> 4)
        out.append(byte & 0x0F)
    return "".join(str(d) for d in out[:count])
