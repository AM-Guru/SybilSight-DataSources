#!/usr/bin/env python3
"""birthday-almanac — the per-date extras that cannot be computed.

Everything a birthdate yields deterministically — star sign, element, ruling
planet, Chinese animal and element, birthstone, birth flower, weekday, days
alive, life-path number, generation — is computed ON DEVICE by
``BirthdayFactsCalculator``. None of it is in this file, and none of it should
be: a table of 44,000 precomputed rows would be a slower, staler way to get the
same answer, and it would be wrong the moment a leap year moved a weekday.

What IS here is the residue that genuinely requires a lookup: a short "that day"
line per calendar date, drawn from the day-in-history selection. The dataset is
small, it is bundled with the app, and its absence only removes the one extra
line — every other birthday fact still works offline on a fresh install.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BUILD_DIR, log, new_container, package, write_meta  # noqa: E402

DATASET_ID = "birthday-almanac"


def load_from_on_this_day() -> list[dict]:
    """Derive from the already-built on-this-day container rather than re-fetching
    366 feeds — the two datasets must not be able to disagree about a date."""
    import sqlite3

    source = BUILD_DIR / "on-this-day.sqlite3"
    if not source.exists():
        raise SystemExit(
            "build on-this-day first: python3 builders/build_on_this_day.py"
        )
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    # Prefer a non-sensitive entry per date. A birthday reading is a light
    # moment; leading it with an atrocity because that entry happened to rank
    # highest is a failure of the product, not of the data. The full list stays
    # available through the on-this-day dataset itself.
    rows = connection.execute(
        """
        SELECT month, day, year, headline FROM event e
        WHERE e.rowid = (
            SELECT x.rowid FROM event x
            WHERE x.month = e.month AND x.day = e.day
            ORDER BY x.sensitive ASC, x.significance DESC
            LIMIT 1
        )
        GROUP BY month, day
        """
    ).fetchall()
    connection.close()

    records = []
    for month, day, year, headline in rows:
        records.append(
            {
                "month_day": f"{month:02d}-{day:02d}",
                "year": None,
                "note": f"{year}: {headline}"[:300],
            }
        )
    log(f"{len(records)} calendar-day notes")
    return records


def build() -> None:
    records = load_from_on_this_day()
    if len(records) < 366:
        raise SystemExit(f"only {len(records)} of 366 calendar days covered")

    with new_container(DATASET_ID) as connection:
        connection.executescript(
            """
            CREATE TABLE almanac (
                month_day TEXT NOT NULL,
                year INTEGER,
                note TEXT NOT NULL
            );
            CREATE INDEX almanac_lookup ON almanac (month_day, year);
            """
        )
        connection.executemany(
            "INSERT INTO almanac (month_day, year, note) VALUES (:month_day, :year, :note)",
            records,
        )
        write_meta(
            connection,
            DATASET_ID,
            record_count=len(records),
            source="Derived from the on-this-day container",
            license="CC BY-SA 4.0",
            note="Sign, stone, flower, Chinese zodiac, weekday and days alive are computed on device.",
        )

    package(DATASET_ID, compression="gzip")


if __name__ == "__main__":
    build()
