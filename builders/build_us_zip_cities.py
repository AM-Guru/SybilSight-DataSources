#!/usr/bin/env python3
"""us-zip-cities — every US ZIP code, its own place, and its biggest city.

Source: GeoNames postal codes (US.zip) joined to GeoNames cities15000, both
CC BY 4.0.

"Biggest city" is a judgement call, so it is written down here rather than left
implicit in the code:

  1. Prefer the largest-population city within PRIMARY_RADIUS_KM.
  2. Break ties toward the SAME STATE — a New Jersey ZIP twenty minutes from
     Manhattan should still answer "Newark" for most performing purposes, and a
     spectator who says "I'm near Newark" is not wrong.
  3. If nothing qualifies inside the primary radius, fall back to the largest
     city within FALLBACK_RADIUS_KM, then to the nearest city at any distance.

That last rule is what keeps rural Alaska and Nevada answerable at all.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    download,
    log,
    new_container,
    package,
    unzip_member,
    write_meta,
)

DATASET_ID = "us-zip-cities"

POSTAL_URL = "https://download.geonames.org/export/zip/US.zip"
CITIES_URL = "https://download.geonames.org/export/dump/cities15000.zip"

PRIMARY_RADIUS_KM = 80.0
FALLBACK_RADIUS_KM = 250.0
# Same-state preference expressed as a population multiplier rather than a hard
# filter, so a genuinely dominant out-of-state metro (Kansas City for a Kansas
# ZIP) still wins.
SAME_STATE_BONUS = 1.6


def load_cities() -> list[dict]:
    archive = download(CITIES_URL)
    path = unzip_member(archive, "cities15000.txt")
    cities = []
    with open(path, encoding="utf-8") as handle:
        for row in csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(row) < 18 or row[8] != "US":
                continue
            try:
                cities.append(
                    {
                        "name": row[1],
                        "state": row[10],
                        "lat": float(row[4]),
                        "lon": float(row[5]),
                        "population": int(row[14] or 0),
                        "timezone": row[17],
                    }
                )
            except ValueError:
                continue
    cities.sort(key=lambda c: -c["population"])
    log(f"{len(cities):,} US cities with population >= 15,000")
    return cities


def load_zips() -> list[dict]:
    archive = download(POSTAL_URL)
    path = unzip_member(archive, "US.txt")
    seen = set()
    zips = []
    with open(path, encoding="utf-8") as handle:
        for row in csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(row) < 11 or row[0] != "US":
                continue
            code = row[1].strip()
            # GeoNames lists a handful of codes twice (a code spanning two
            # places); the first row is the primary and a UNIQUE index on zip
            # would otherwise reject the rest.
            if not code or code in seen:
                continue
            try:
                lat, lon = float(row[9]), float(row[10])
            except ValueError:
                continue
            seen.add(code)
            zips.append(
                {
                    "zip": code,
                    "place": row[2].strip(),
                    "state": row[3].strip(),
                    "state_code": row[4].strip(),
                    "county": row[5].strip(),
                    "lat": lat,
                    "lon": lon,
                }
            )
    log(f"{len(zips):,} unique US ZIP codes")
    return zips


def assign_major_cities(zips: list[dict], cities: list[dict]) -> None:
    """Vectorised nearest/biggest search.

    41,490 ZIPs against ~3,700 cities is 153M distance evaluations. In pure
    Python that is minutes; as one numpy broadcast per ZIP-chunk it is seconds,
    which keeps the builder re-runnable while the ranking rules are tuned.
    """
    city_lat = np.radians(np.array([c["lat"] for c in cities]))
    city_lon = np.radians(np.array([c["lon"] for c in cities]))
    city_pop = np.array([c["population"] for c in cities], dtype=np.float64)
    city_state = np.array([c["state"] for c in cities])

    chunk = 512
    radius = 6371.0088
    for start in range(0, len(zips), chunk):
        block = zips[start : start + chunk]
        lat = np.radians(np.array([z["lat"] for z in block]))[:, None]
        lon = np.radians(np.array([z["lon"] for z in block]))[:, None]

        dlat = city_lat[None, :] - lat
        dlon = city_lon[None, :] - lon
        a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(city_lat[None, :]) * np.sin(dlon / 2) ** 2
        distance = 2 * radius * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

        for index, record in enumerate(block):
            row = distance[index]
            same_state = city_state == record["state_code"]
            weighted = city_pop * np.where(same_state, SAME_STATE_BONUS, 1.0)

            pick = None
            for limit in (PRIMARY_RADIUS_KM, FALLBACK_RADIUS_KM):
                mask = row <= limit
                if mask.any():
                    candidate = int(np.argmax(np.where(mask, weighted, -1.0)))
                    pick = candidate
                    break
            if pick is None:
                pick = int(np.argmin(row))

            city = cities[pick]
            record["major_city"] = city["name"]
            record["major_city_state"] = city["state"]
            record["major_city_population"] = city["population"]
            record["major_city_km"] = round(float(row[pick]), 1)
            # The ZIP's own timezone is not in the postal file; the nearest
            # city's is a faithful stand-in at this granularity.
            nearest = int(np.argmin(row))
            record["timezone"] = cities[nearest]["timezone"]

        if start % (chunk * 20) == 0:
            log(f"  matched {min(start + chunk, len(zips)):,}/{len(zips):,}")


def build() -> None:
    cities = load_cities()
    zips = load_zips()
    assign_major_cities(zips, cities)

    with new_container(DATASET_ID) as connection:
        connection.executescript(
            """
            CREATE TABLE zip (
                zip TEXT PRIMARY KEY,
                place TEXT NOT NULL,
                state TEXT NOT NULL,
                state_code TEXT NOT NULL,
                county TEXT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                major_city TEXT,
                major_city_state TEXT,
                major_city_population INTEGER,
                major_city_km REAL,
                timezone TEXT
            );
            CREATE INDEX zip_place ON zip (place);
            CREATE INDEX zip_major_city ON zip (major_city);
            """
        )
        connection.executemany(
            """
            INSERT INTO zip (zip, place, state, state_code, county, latitude, longitude,
                             major_city, major_city_state, major_city_population,
                             major_city_km, timezone)
            VALUES (:zip, :place, :state, :state_code, :county, :lat, :lon,
                    :major_city, :major_city_state, :major_city_population,
                    :major_city_km, :timezone)
            """,
            zips,
        )
        write_meta(
            connection,
            DATASET_ID,
            record_count=len(zips),
            source="GeoNames postal codes + cities15000",
            license="CC BY 4.0",
            primary_radius_km=PRIMARY_RADIUS_KM,
        )

    package(DATASET_ID, compression="gzip")


if __name__ == "__main__":
    build()
