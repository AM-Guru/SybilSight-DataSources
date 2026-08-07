#!/usr/bin/env python3
"""constant-digits — deep decimal expansions of pi, e, phi, and the small roots.

Why this exists as a dataset rather than a computation: the app generates
10,000 digits on device in a fraction of a second, which finds any two- or
three-digit run. But a six-digit run (a birthday, DDMMYY) first appears around
the millionth decimal place, and an eight-digit one around the hundred
millionth. Computing a million digits of pi with the app's scalar-only big
arithmetic would take minutes; shipping them is a few hundred kilobytes.

Everything here is computed with Python's arbitrary-precision integers, using
the same *algorithms* the Swift side uses, and the first 10,000 digits of each
constant are cross-checked against the on-device generator by
`tools/validate_catalog.py`. A hardcoded digit string nobody verifies is exactly
the failure mode this feature cannot survive: one transposed digit silently
shifts every position the app reports.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, new_container, package, write_meta  # noqa: E402

DATASET_ID = "constant-digits"

# A million places each for pi and e, fewer for the roots (nobody asks how deep
# their birthday sits in sqrt(3)). Each digit is one byte of text; SQLite plus
# gzip brings the whole thing well under a megabyte on the wire.
DIGIT_COUNTS = {
    "pi": 1_000_000,
    "e": 1_000_000,
    "phi": 200_000,
    "sqrt2": 200_000,
    "sqrt3": 100_000,
    "sqrt5": 100_000,
}

# Rows are chunked so a search can stream without loading a megabyte string,
# and so the app can page to a match's neighbourhood cheaply.
CHUNK = 10_000


# The app uses Machin's arctan series, which is O(n^2) and perfectly fine for
# the 10,000 places it computes on device. At a million places it is hopeless:
# ~700,000 terms each dividing a million-digit integer. Both constants here use
# BINARY SPLITTING instead, which is quasi-linear and finishes in seconds.
#
# The two implementations are cross-checked against each other by
# `validate_catalog.py` (and against published prefixes below), so the fast
# path cannot silently diverge from the one the app ships.

import math
import sys

sys.setrecursionlimit(10_000)


def compute_pi(digits: int) -> str:
    """Chudnovsky with binary splitting."""
    guard = 20
    precision = digits + guard
    C3_OVER_24 = 640320**3 // 24

    def split(a: int, b: int):
        if b - a == 1:
            if a == 0:
                p = q = 1
            else:
                p = (6 * a - 5) * (2 * a - 1) * (6 * a - 1)
                q = a * a * a * C3_OVER_24
            t = p * (13591409 + 545140134 * a)
            if a & 1:
                t = -t
            return p, q, t
        mid = (a + b) // 2
        p1, q1, t1 = split(a, mid)
        p2, q2, t2 = split(mid, b)
        return p1 * p2, q1 * q2, q2 * t1 + p1 * t2

    # Chudnovsky yields ~14.18 digits per term.
    terms = int(precision / 14.181647462725477) + 2
    _, q, t = split(0, terms)
    root = math.isqrt(10005 * 10 ** (2 * precision))
    value = (q * 426880 * root) // t
    return str(value)[1 : 1 + digits]        # drop the leading 3


def compute_e(digits: int) -> str:
    """e = sum 1/k!, by binary splitting on the partial numerator/denominator."""
    guard = 20
    precision = digits + guard

    def split(a: int, b: int):
        """Returns (p, q) with sum_{k=a}^{b-1} 1/k! == p/q, relative to a!."""
        if b - a == 1:
            return 1, b
        mid = (a + b) // 2
        p1, q1 = split(a, mid)
        p2, q2 = split(mid, b)
        return p1 * q2 + p2, q1 * q2

    # k! passes 10^precision at roughly this many terms (Stirling, loosely).
    terms = 10
    while math.lgamma(terms + 1) / math.log(10) < precision:
        terms = int(terms * 1.5) + 10
    p, q = split(0, terms)
    value = 10**precision + (p * 10**precision) // q
    return str(value)[1 : 1 + digits]        # drop the leading 2


def integer_sqrt_scaled(value: int, digits: int) -> int:
    """isqrt(value * 10**(2*digits)) — value's root, scaled by 10**digits."""
    import math

    return math.isqrt(value * 10 ** (2 * digits))


def compute_sqrt(value: int, digits: int) -> str:
    guard = 20
    root = integer_sqrt_scaled(value, digits + guard)
    text = str(root)
    # One integer digit for 2, 3, and 5 (roots are 1.41…, 1.73…, 2.23…).
    return text[1 : 1 + digits]


def compute_phi(digits: int) -> str:
    guard = 20
    scale = digits + guard
    root5 = integer_sqrt_scaled(5, scale)
    value = (root5 + 10**scale) // 2
    return str(value)[1 : 1 + digits]


BUILDERS = {
    "pi": compute_pi,
    "e": compute_e,
    "phi": compute_phi,
    "sqrt2": lambda d: compute_sqrt(2, d),
    "sqrt3": lambda d: compute_sqrt(3, d),
    "sqrt5": lambda d: compute_sqrt(5, d),
}

# Cross-check: the first 50 places of each, from published expansions. If a
# builder ever drifts, it fails here rather than shipping wrong positions.
KNOWN_PREFIXES = {
    "pi": "14159265358979323846264338327950288419716939937510",
    "e": "71828182845904523536028747135266249775724709369995",
    "phi": "61803398874989484820458683436563811772030917980576",
    "sqrt2": "41421356237309504880168872420969807856967187537694",
    "sqrt3": "73205080756887729352744634150587236694280525381038",
    "sqrt5": "23606797749978969640917366873127623544061835961152",
}


def build() -> None:
    expansions: dict[str, str] = {}
    for name, count in DIGIT_COUNTS.items():
        log(f"computing {count:,} digits of {name}")
        digits = BUILDERS[name](count)
        if len(digits) < count:
            raise SystemExit(f"{name}: got {len(digits)} digits, wanted {count}")
        expected = KNOWN_PREFIXES[name]
        if not digits.startswith(expected):
            raise SystemExit(
                f"{name}: computed expansion does not match the published prefix\n"
                f"  got  {digits[:50]}\n  want {expected}"
            )
        expansions[name] = digits
        log(f"  verified against the published first 50 places")

    with new_container(DATASET_ID) as connection:
        connection.executescript(
            """
            CREATE TABLE constant (
                name TEXT PRIMARY KEY,
                digit_count INTEGER NOT NULL,
                chunk_size INTEGER NOT NULL
            );
            CREATE TABLE digit_chunk (
                name TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                -- 1-based decimal place of this chunk's first digit.
                start_place INTEGER NOT NULL,
                digits TEXT NOT NULL,
                PRIMARY KEY (name, chunk_index)
            );
            CREATE INDEX digit_chunk_place ON digit_chunk (name, start_place);
            """
        )
        for name, digits in expansions.items():
            connection.execute(
                "INSERT INTO constant (name, digit_count, chunk_size) VALUES (?, ?, ?)",
                (name, len(digits), CHUNK),
            )
            rows = []
            for index in range(0, len(digits), CHUNK):
                rows.append((name, index // CHUNK, index + 1, digits[index : index + CHUNK]))
            connection.executemany(
                "INSERT INTO digit_chunk (name, chunk_index, start_place, digits) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            log(f"  stored {name}: {len(rows):,} chunks")

        write_meta(
            connection,
            DATASET_ID,
            record_count=sum(len(d) for d in expansions.values()),
            chunk_size=CHUNK,
            constants=",".join(sorted(expansions)),
            source="Computed with Machin's formula, the exponential series, and integer square roots",
            license="CC0 1.0",
            note="Mathematical constants are facts, not authored works.",
        )

    package(DATASET_ID, compression="gzip")


if __name__ == "__main__":
    build()
