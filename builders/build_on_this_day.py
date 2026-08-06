#!/usr/bin/env python3
"""on-this-day — the notable events of every calendar day, ~120 years deep.

Source: the English Wikipedia "Selected anniversaries" / day-article event
lists, via the REST feed (CC BY-SA 4.0).

366 requests, one per calendar day, each returning that day's curated events
across all years. The feed is already an editorial selection ("what Wikipedia
puts on its front page for this date"), which is exactly the right filter — a
raw dump of every dated statement would bury the moon landing under municipal
boundary changes.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CACHE_DIR, USER_AGENT, log, new_container, package, write_meta  # noqa: E402

DATASET_ID = "on-this-day"

FEED = "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{month:02d}/{day:02d}"

EARLIEST_YEAR = date.today().year - 125
# "events" is the general list; the others are the categorised front-page
# selections. Ranked in that order because a "selected" entry is, by
# definition, the one Wikipedia editors judged most notable for the date.
SECTION_WEIGHTS = {"selected": 100, "events": 60, "births": 30, "deaths": 25, "holidays": 20}

# Marks entries a light-hearted "what happened on your birthday" reveal should
# not lead with. The data is NOT filtered — a history question still returns
# everything, in full — but a birthday reading picks a non-sensitive entry
# first. Recency ranking alone put a mass shooting at the top of 20 July, which
# is not something to hand a performer mid-routine without warning.
SENSITIVE_PATTERNS = [
    "massacre", "shooting", "shot dead", "bomb", "detonat", "terrorist",
    "terror attack", "suicide attack", "genocide", "assassinat", "murder",
    "killed", "kills", "death toll", "atrocity", "war crime", "executed",
    "lynch", "hostage", "hijack", "famine", "epidemic claimed", "torture",
    "crashed, killing", "sank, killing", "died in", "casualties",
]


def fetch_day(month: int, day: int) -> dict:
    cache = CACHE_DIR / "onthisday" / f"{month:02d}-{day:02d}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        return json.loads(cache.read_text())

    request = urllib.request.Request(
        FEED.format(month=month, day=day),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
            cache.write_text(json.dumps(payload))
            return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            log(f"  retry {month:02d}-{day:02d}: {error}")
            time.sleep(3 * (attempt + 1))
    log(f"  giving up on {month:02d}-{day:02d}")
    return {}


def build() -> None:
    rows: list[dict] = []
    # A leap year so 29 February is covered.
    cursor = date(2024, 1, 1)
    while cursor.year == 2024:
        payload = fetch_day(cursor.month, cursor.day)
        for section, weight in SECTION_WEIGHTS.items():
            for entry in payload.get(section, []):
                year = entry.get("year")
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                if year is None or not isinstance(year, int) or year < EARLIEST_YEAR:
                    continue
                pages = entry.get("pages") or []
                detail = ""
                if pages:
                    extract = pages[0].get("extract") or ""
                    detail = extract.strip()[:400]

                lowered = text.lower()
                sensitive = any(pattern in lowered for pattern in SENSITIVE_PATTERNS)
                # Notability proxy: how many Wikipedia articles the entry links.
                # Deliberately NOT recency — ranking by year made the most
                # recent event win every date, which systematically surfaced
                # modern tragedies over the landmark events of the century.
                linked = min(len(pages), 6)
                rows.append(
                    {
                        "year": year,
                        "month": cursor.month,
                        "day": cursor.day,
                        "headline": text[:400],
                        "detail": detail,
                        "category": section,
                        "sensitive": 1 if sensitive else 0,
                        "significance": weight * 1000 + linked * 50 + (10 if detail else 0),
                    }
                )
        if cursor.day == 1:
            log(f"collected through {cursor.month:02d}-01 ({len(rows):,} events)")
        cursor += timedelta(days=1)

    if len(rows) < 10_000:
        raise SystemExit(f"only {len(rows)} events collected — refusing to publish a thin dataset")

    with new_container(DATASET_ID) as connection:
        connection.executescript(
            """
            CREATE TABLE event (
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                headline TEXT NOT NULL,
                detail TEXT,
                category TEXT,
                sensitive INTEGER NOT NULL DEFAULT 0,
                significance INTEGER NOT NULL
            );
            CREATE INDEX event_day ON event (month, day, significance DESC);
            CREATE INDEX event_day_gentle ON event (month, day, sensitive, significance DESC);
            CREATE INDEX event_full ON event (year, month, day, significance DESC);
            CREATE INDEX event_year ON event (year, significance DESC);
            """
        )
        connection.executemany(
            """
            INSERT INTO event (year, month, day, headline, detail, category, sensitive, significance)
            VALUES (:year, :month, :day, :headline, :detail, :category, :sensitive, :significance)
            """,
            rows,
        )
        write_meta(
            connection,
            DATASET_ID,
            record_count=len(rows),
            earliest_year=EARLIEST_YEAR,
            source="Wikimedia on-this-day feed (English Wikipedia)",
            license="CC BY-SA 4.0",
        )

    package(DATASET_ID, compression="gzip")


if __name__ == "__main__":
    build()
