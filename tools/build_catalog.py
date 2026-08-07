#!/usr/bin/env python3
"""Assemble manifest/catalog.json from whatever has been packaged into dist/.

This is the file the app fetches. Its shape is decoded by
``ReferenceDataCatalogDocument`` in LocalPackages/ReferenceData — the two must
stay in step, and ``tools/validate_catalog.py`` checks that they have.

Datasets that were not rebuilt keep their existing published entry, so a
one-dataset refresh does not silently retract the other six.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
MANIFEST_PATH = REPO_ROOT / "manifest" / "catalog.json"

# Files under 100 MB can live in the repo and be served from raw. Anything
# larger CANNOT: GitHub rejects blobs over 100 MB outright, and raw has the same
# ceiling. Those ship as Release assets instead, which allow up to 2 GB each —
# which is also why the deep tiers are split into parts rather than one file.
RAW_BASE = "https://raw.githubusercontent.com/AM-Guru/SybilSight-DataSources/main/dist"
RELEASE_ASSET_BASE = "https://github.com/AM-Guru/SybilSight-DataSources/releases/download"

# The hard limits we are publishing against.
GITHUB_BLOB_LIMIT = 100 * 1000 * 1000
GITHUB_ASSET_LIMIT = 2 * 1000 * 1000 * 1000

# Anything heavier than this is never bundled into the app, whatever the
# descriptor says. Mirrors ReferenceDatasetDescriptor.bundleCeilingBytes.
BUNDLE_CEILING = 20 * 1000 * 1000


def asset_url(file_name: str, version: str) -> str:
    """Where a published artefact is actually fetched from."""
    return f"{RELEASE_ASSET_BASE}/data-{version}/{file_name}"


def download_url(file_name: str, size: int, version: str) -> str:
    return (
        f"{RAW_BASE}/{file_name}" if size < GITHUB_BLOB_LIMIT
        else asset_url(file_name, version)
    )

MANIFEST_VERSION = 1

# Everything the app shows about a dataset except its release, which comes from
# the packaged artefact. Kept here (not in each builder) so the presentation
# copy can be edited without re-running a multi-hour build.
DESCRIPTORS = {
    "us-zip-cities": {
        "title": "US ZIP Codes & Cities",
        "summary": "Every US ZIP code with its own place, county, state, time zone, and the largest city nearby.",
        "category": "places",
        "symbolName": "mappin.and.ellipse",
        "attribution": "GeoNames postal codes and cities15000",
        "license": "CC BY 4.0",
        "sourceURL": "https://www.geonames.org/",
        "bundled": True,
    },
    "birthday-almanac": {
        "title": "Birthday Almanac",
        "summary": "The per-date extras behind birthday readings. Sign, stone, flower, Chinese zodiac, weekday and days alive are always computed on device — this adds the day's notable event.",
        "category": "calendar",
        "symbolName": "gift",
        "attribution": "Derived from the Wikimedia on-this-day selection",
        "license": "CC BY-SA 4.0",
        "sourceURL": "https://en.wikipedia.org/",
        "bundled": True,
    },
    "on-this-day": {
        "title": "This Day in History",
        "summary": "Notable events for every calendar day across the past 125 years, with the front-page selections ranked first.",
        "category": "calendar",
        "symbolName": "calendar.badge.clock",
        "attribution": "Wikimedia on-this-day feed",
        "license": "CC BY-SA 4.0",
        "sourceURL": "https://en.wikipedia.org/",
        "bundled": False,
    },
    "name-meanings": {
        "title": "Name Meanings",
        "summary": "Meaning and origin for the 50,000 most-borne first and family names.",
        "category": "language",
        "symbolName": "character.book.closed.fill",
        "attribution": "Wikidata given-name and family-name items",
        "license": "CC0 1.0",
        "sourceURL": "https://www.wikidata.org/",
        "bundled": False,
    },
    "world-leaders": {
        "title": "World Leaders by Year",
        "summary": "Heads of state and heads of government for every country and year. Transition years name every officeholder.",
        "category": "people",
        "symbolName": "building.columns",
        "attribution": "Wikidata P35 / P6 statements",
        "license": "CC0 1.0",
        "sourceURL": "https://www.wikidata.org/",
        "bundled": False,
    },
    "celebrities": {
        "title": "Celebrities",
        "summary": "The 50,000 most notable people: birth and death dates, days alive, partner, residence, notable work, and awards.",
        "category": "people",
        "symbolName": "star.circle",
        "attribution": "Wikidata facts with Wikipedia lead descriptions",
        "license": "CC0 1.0 / CC BY-SA 4.0",
        "sourceURL": "https://www.wikidata.org/",
        "bundled": False,
    },
    "constant-digits": {
        "title": "Constant Digits",
        "summary": "Five million places each of pi and e, a million each of the golden ratio and the small roots. Ships with the app and settles any six-digit run — every DDMMYY date.",
        "category": "calendar",
        "symbolName": "number",
        "attribution": "Computed with binary-splitting Chudnovsky over GMP",
        "license": "CC0 1.0",
        "sourceURL": "https://github.com/AM-Guru/SybilSight-DataSources",
        "bundled": True,
    },
    "constant-digits-7": {
        "title": "Deep Digits — 7-figure runs",
        "summary": "Pi and e to 46 million places each. Settles any seven-digit run, such as a date with a single-digit day or month.",
        "category": "calendar",
        "symbolName": "number",
        "attribution": "Computed with binary-splitting Chudnovsky over GMP",
        "license": "CC0 1.0",
        "sourceURL": "https://github.com/AM-Guru/SybilSight-DataSources",
        "bundled": False,
    },
    "constant-digits-8": {
        "title": "Deep Digits — full birthdates",
        "summary": "Pi to 461 million places. This is the depth at which a full DDMMYYYY birthdate is 99% likely to appear — the shallower sets simply cannot answer it.",
        "category": "calendar",
        "symbolName": "number",
        "attribution": "Computed with binary-splitting Chudnovsky over GMP",
        "license": "CC0 1.0",
        "sourceURL": "https://github.com/AM-Guru/SybilSight-DataSources",
        "bundled": False,
    },
    "constant-digits-9": {
        "title": "Deep Digits — 9-figure runs",
        "summary": "Pi to 4.6 billion places, in parts. Settles any nine-digit run, and covers roughly a third of ten-digit ones. Wi-Fi and 5 GB of free space.",
        "category": "calendar",
        "symbolName": "number",
        "attribution": "Computed with binary-splitting Chudnovsky over GMP",
        "license": "CC0 1.0",
        "sourceURL": "https://github.com/AM-Guru/SybilSight-DataSources",
        "bundled": False,
    },
    "wikipedia-en": {
        "title": "Offline Wikipedia (English)",
        "summary": "Lead extracts for every English Wikipedia article, with offline full-text search. Large — Wi-Fi recommended.",
        "category": "encyclopedia",
        "symbolName": "books.vertical.fill",
        "attribution": "English Wikipedia abstracts dump",
        "license": "CC BY-SA 4.0",
        "sourceURL": "https://en.wikipedia.org/",
        "bundled": False,
    },
}

ORDER = [
    "us-zip-cities", "birthday-almanac", "on-this-day", "name-meanings",
    "world-leaders", "celebrities", "constant-digits",
    "constant-digits-7", "constant-digits-8", "constant-digits-9",
    "wikipedia-en",
]


def load_existing() -> dict[str, dict]:
    if not MANIFEST_PATH.exists():
        return {}
    document = json.loads(MANIFEST_PATH.read_text())
    return {entry["id"]: entry for entry in document.get("datasets", [])}


def record_counts() -> dict[str, int]:
    """Read the row count each builder wrote into its container's meta table."""
    import sqlite3

    counts = {}
    for dataset_id in ORDER:
        path = REPO_ROOT / "build" / f"{dataset_id}.sqlite3"
        if not path.exists():
            continue
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'record_count'"
            ).fetchone()
            connection.close()
            if row:
                counts[dataset_id] = int(row[0])
        except sqlite3.Error:
            continue
    return counts


def build() -> None:
    existing = load_existing()
    counts = record_counts()
    datasets = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    for dataset_id in ORDER:
        release_path = DIST_DIR / f"{dataset_id}.release.json"
        descriptor = DESCRIPTORS[dataset_id]

        if not release_path.exists():
            if dataset_id in existing:
                print(f"  keeping published entry for {dataset_id} (not rebuilt)")
                datasets.append(existing[dataset_id])
            else:
                print(f"  skipping {dataset_id} (never built)")
            continue

        release = json.loads(release_path.read_text())
        version = release["version"]

        parts = release.get("parts") or [{
            "fileName": release["fileName"],
            "downloadBytes": release["downloadBytes"],
            "installedBytes": release["installedBytes"],
            "sha256": release["sha256"],
        }]
        for part in parts:
            if part["downloadBytes"] > GITHUB_ASSET_LIMIT:
                raise SystemExit(
                    f"{dataset_id}: part {part['fileName']} is "
                    f"{part['downloadBytes']:,} bytes, over GitHub's 2 GB asset "
                    f"limit — raise the split count in build_constant_tiers.py"
                )
        emitted_parts = [
            {
                "index": index,
                "fileName": part["fileName"],
                "downloadURL": download_url(part["fileName"], part["downloadBytes"], version),
                "downloadBytes": part["downloadBytes"],
                "installedBytes": part["installedBytes"],
                "sha256": part["sha256"],
            }
            for index, part in enumerate(parts)
        ]

        # Size decides this, not the descriptor. A dataset that outgrew the
        # ceiling and kept `bundled: True` would send the app looking inside its
        # own binary for a file that is not there.
        bundled = descriptor["bundled"] and release["installedBytes"] <= BUNDLE_CEILING
        if descriptor["bundled"] and not bundled:
            print(
                f"  {dataset_id}: {release['installedBytes']:,} bytes installed is over the "
                f"{BUNDLE_CEILING:,} bundle ceiling — publishing as an optional download"
            )

        entry = {
            "id": dataset_id,
            "title": descriptor["title"],
            "summary": descriptor["summary"],
            "category": descriptor["category"],
            "symbolName": descriptor["symbolName"],
            "attribution": descriptor["attribution"],
            "license": descriptor["license"],
            "sourceURL": descriptor["sourceURL"],
            "bundled": bundled,
            "recordCountEstimate": counts.get(dataset_id, 0),
            "release": {
                "version": release["version"],
                "schemaVersion": release["schemaVersion"],
                "downloadURL": emitted_parts[0]["downloadURL"],
                "downloadBytes": release["downloadBytes"],
                "installedBytes": release["installedBytes"],
                "sha256": release["sha256"],
                "compression": release["compression"],
                "publishedAt": now,
                "releaseNotes": f"Rebuilt {release['version']}.",
                "parts": emitted_parts if len(emitted_parts) > 1 else [],
            },
        }
        # Do not churn publishedAt when the bytes are identical — the app keys
        # its update check off `version`, but a moving timestamp still makes the
        # manifest diff noisy and hides the releases that really changed.
        previous = existing.get(dataset_id)
        if previous and previous["release"].get("sha256") == release["sha256"]:
            entry["release"]["publishedAt"] = previous["release"]["publishedAt"]
            entry["release"]["version"] = previous["release"]["version"]
            entry["release"]["releaseNotes"] = previous["release"].get("releaseNotes", "")
        datasets.append(entry)

    document = {
        "manifestVersion": MANIFEST_VERSION,
        "generatedAt": now,
        "datasets": datasets,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(f"wrote {MANIFEST_PATH} with {len(datasets)} dataset(s)")


if __name__ == "__main__":
    build()
    sys.exit(0)
