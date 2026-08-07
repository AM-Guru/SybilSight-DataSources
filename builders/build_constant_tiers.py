#!/usr/bin/env python3
"""Deep constant expansions, tiered by the pattern length they can answer.

The tiers are not arbitrary sizes — each one is the depth at which a run of a
given length is 99% likely to have appeared. For a random k-digit string
scanned over n places, P(hit) = 1 - e^(-n/10^k), so 99% needs n = ln(100)·10^k.
That single formula sets every number below:

    pattern   depth for 99%      packaged      answers
    6 digits        4,605,170        1.9 MB     DDMMYY / MMDDYY dates
    7 digits       46,051,701       19.4 MB
    8 digits      460,517,018      193.9 MB     DDMMYYYY full birthdates
    9 digits    4,605,170,185        1.94 GB
   10 digits   46,051,701,860       19.4 GB     — over any sane ceiling

Ten digits is therefore not offered: it would need ~19 GB, an order of
magnitude past the 2 GB ceiling. The nine-digit set does give partial ten-digit
coverage (about 37%), and the catalog says so rather than implying otherwise.

Parts: a tier over ~1 billion digits is split into standalone containers, each
a valid database in its own right. This is deliberate — a single 2 GB download
that fails at 90% costs the whole thing, and expanding a 2 GB archive needs
4 GB of free space on the device at once. Per-part containers install one at a
time and resume at part granularity.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BUILD_DIR, DIST_DIR, build_stamp, ensure_dirs, log  # noqa: E402
from constant_math import expansion, pack, unpack  # noqa: E402

SCHEMA_VERSION = 2

# ln(100) — the multiplier that turns 10^k into a 99%-coverage depth.
COVERAGE_99 = math.log(100)


def depth_for(pattern_digits: int) -> int:
    return int(COVERAGE_99 * 10**pattern_digits)


TIERS: dict[str, dict] = {
    # Bundled. Small enough to ship inside the app, and it settles every
    # six-digit date, which is the overwhelmingly common case.
    "constant-digits": {
        "counts": {
            "pi": 5_000_000, "e": 5_000_000, "phi": 1_000_000,
            "sqrt2": 1_000_000, "sqrt3": 1_000_000, "sqrt5": 1_000_000,
        },
        "answers": 6,
    },
    "constant-digits-7": {
        "counts": {"pi": depth_for(7), "e": depth_for(7)},
        "answers": 7,
    },
    "constant-digits-8": {
        "counts": {"pi": depth_for(8)},
        "answers": 8,
    },
    "constant-digits-9": {
        "counts": {"pi": depth_for(9)},
        "answers": 9,
    },
}

# Digits per BLOB. One megabyte of packed bytes: big enough that chunk joins are
# rare during a scan, small enough that the reader never holds much at once.
CHUNK_DIGITS = 2_000_000

# Split a tier once it passes this. ~421 MB packaged per part at the measured
# 0.421 bytes/digit.
PART_DIGITS_MAX = 1_000_000_000


def part_plan(total: int) -> list[tuple[int, int]]:
    """(start_place, digit_count) per part, 1-based and contiguous."""
    if total <= PART_DIGITS_MAX:
        return [(1, total)]
    parts, start = [], 1
    while start <= total:
        take = min(PART_DIGITS_MAX, total - start + 1)
        parts.append((start, take))
        start += take
    return parts


SCHEMA = """
PRAGMA page_size = 8192;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE constant (
    name TEXT PRIMARY KEY,
    -- Total places of this constant across the WHOLE tier, not just this part.
    digit_count INTEGER NOT NULL,
    -- 1-based first place present in this part, and how many it holds.
    part_start INTEGER NOT NULL,
    part_digits INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL
);
CREATE TABLE digit_chunk (
    name TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    -- 1-based absolute decimal place of this chunk's first digit.
    start_place INTEGER NOT NULL,
    -- True digit count; the last chunk may be short, and an odd count leaves a
    -- padding nibble in the final byte.
    digit_count INTEGER NOT NULL,
    packed BLOB NOT NULL,
    PRIMARY KEY (name, chunk_index)
);
CREATE INDEX digit_chunk_place ON digit_chunk (name, start_place);
"""


def write_part(
    path: Path,
    dataset_id: str,
    tier: dict,
    expansions: dict[str, str],
    part_index: int,
    part_count: int,
    window: dict[str, tuple[int, int]],
) -> None:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")

        rows_written = 0
        for name, digits in expansions.items():
            start, count = window[name]
            if count <= 0:
                continue
            connection.execute(
                "INSERT INTO constant (name, digit_count, part_start, part_digits, chunk_size)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, len(digits), start, count, CHUNK_DIGITS),
            )
            index = 0
            offset = start - 1
            end = offset + count
            while offset < end:
                take = min(CHUNK_DIGITS, end - offset)
                slice_ = digits[offset : offset + take]
                connection.execute(
                    "INSERT INTO digit_chunk"
                    " (name, chunk_index, start_place, digit_count, packed)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, index, offset + 1, take, pack(slice_)),
                )
                index += 1
                offset += take
                rows_written += 1

        meta = {
            "dataset_id": dataset_id,
            "schema_version": str(SCHEMA_VERSION),
            "version": build_stamp(),
            "encoding": "bcd2",
            "part_index": str(part_index),
            "part_count": str(part_count),
            "record_count": str(sum(len(v) for v in expansions.values())),
            "answers_pattern_digits": str(tier["answers"]),
        }
        connection.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", sorted(meta.items())
        )
        connection.commit()
    finally:
        connection.close()
    log(f"  part {part_index + 1}/{part_count}: {path.name} ({path.stat().st_size:,} bytes)")


def verify_part(path: Path, expansions: dict[str, str]) -> None:
    """Read the container back and confirm the digits survived the round trip.

    Checks the seams specifically — first chunk, last chunk, and a chunk
    boundary — because an off-by-one in the packing would shift every position
    the app reports and would not be visible in a spot check of the middle.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for name, _, part_start, part_digits, _ in connection.execute(
            "SELECT name, digit_count, part_start, part_digits, chunk_size FROM constant"
        ):
            source = expansions[name]
            rows = connection.execute(
                "SELECT chunk_index, start_place, digit_count, packed FROM digit_chunk"
                " WHERE name = ? ORDER BY chunk_index",
                (name,),
            ).fetchall()
            if not rows:
                raise SystemExit(f"{path.name}: {name} has no chunks")

            total = sum(r[2] for r in rows)
            if total != part_digits:
                raise SystemExit(
                    f"{path.name}: {name} holds {total:,} digits, declared {part_digits:,}"
                )
            for index, start_place, count, blob in (rows[0], rows[len(rows) // 2], rows[-1]):
                got = unpack(blob, count)
                want = source[start_place - 1 : start_place - 1 + count]
                if got != want:
                    raise SystemExit(
                        f"{path.name}: {name} chunk {index} does not round-trip\n"
                        f"  got  {got[:40]}…\n  want {want[:40]}…"
                    )
            # Contiguity: chunk k must start exactly where chunk k-1 ended.
            expected = part_start
            for _, start_place, count, _ in rows:
                if start_place != expected:
                    raise SystemExit(
                        f"{path.name}: {name} has a gap at place {expected:,}"
                        f" (next chunk starts at {start_place:,})"
                    )
                expected += count
    finally:
        connection.close()


def package_part(path: Path, compression: str) -> dict:
    ensure_dirs()
    installed = path.stat().st_size
    if compression == "none":
        target = DIST_DIR / path.name
        shutil.copy2(path, target)
    else:
        target = DIST_DIR / f"{path.name}.gz"
        # mtime=0 so unchanged input produces byte-identical output and the
        # manifest hash does not churn on every rebuild.
        with open(path, "rb") as raw, gzip.GzipFile(target, "wb", compresslevel=6, mtime=0) as gz:
            shutil.copyfileobj(raw, gz, length=1 << 22)

    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return {
        "fileName": target.name,
        "downloadBytes": target.stat().st_size,
        "installedBytes": installed,
        "sha256": digest.hexdigest(),
    }


def build(dataset_id: str) -> None:
    tier = TIERS[dataset_id]
    ensure_dirs()
    started = time.perf_counter()

    expansions: dict[str, str] = {}
    for name, count in tier["counts"].items():
        mark = time.perf_counter()
        log(f"computing {count:,} digits of {name}")
        expansions[name] = expansion(name, count)
        log(f"  done in {time.perf_counter() - mark:,.1f}s, prefix verified")

    longest = max(len(v) for v in expansions.values())
    plan = part_plan(longest)
    log(f"{dataset_id}: {sum(len(v) for v in expansions.values()):,} digits in {len(plan)} part(s)")

    parts = []
    for index, (start, count) in enumerate(plan):
        suffix = "" if len(plan) == 1 else f".part{index + 1}"
        path = BUILD_DIR / f"{dataset_id}{suffix}.sqlite3"
        window = {}
        for name, digits in expansions.items():
            first = min(start, len(digits) + 1)
            window[name] = (first, max(0, min(count, len(digits) - first + 1)))
        write_part(path, dataset_id, tier, expansions, index, len(plan), window)
        verify_part(path, expansions)
        parts.append(package_part(path, "gzip"))
        log(f"  packaged {parts[-1]['fileName']} ({parts[-1]['downloadBytes']:,} bytes)")

    release = {
        "datasetId": dataset_id,
        "version": build_stamp(),
        "schemaVersion": SCHEMA_VERSION,
        "compression": "gzip",
        "answersPatternDigits": tier["answers"],
        "recordCount": sum(len(v) for v in expansions.values()),
        "parts": parts,
        "downloadBytes": sum(p["downloadBytes"] for p in parts),
        "installedBytes": sum(p["installedBytes"] for p in parts),
        "sha256": parts[0]["sha256"] if len(parts) == 1 else combined_hash(parts),
        "fileName": parts[0]["fileName"],
    }
    (DIST_DIR / f"{dataset_id}.release.json").write_text(json.dumps(release, indent=2) + "\n")
    log(
        f"{dataset_id}: {release['downloadBytes']:,} transfer / "
        f"{release['installedBytes']:,} installed "
        f"in {time.perf_counter() - started:,.1f}s"
    )


def combined_hash(parts: list[dict]) -> str:
    """A hash over the parts' hashes, so a multi-part set has one identity."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part["sha256"].encode())
    return digest.hexdigest()


if __name__ == "__main__":
    targets = sys.argv[1:] or ["constant-digits"]
    for target in targets:
        if target not in TIERS:
            raise SystemExit(f"unknown tier {target}; known: {', '.join(TIERS)}")
        build(target)
