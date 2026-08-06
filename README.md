# SybilSight Data Sources

Public reference datasets for the [Sybil Sight](https://sybilsight.com) app and
the Sybil Terminal Mac companion.

Everything here is derived from openly licensed sources — GeoNames, Wikidata,
Wikipedia, and Wiktionary — packaged as SQLite containers and published with a
manifest the app reads to offer downloads and updates.

**Nothing in this repository is proprietary Sybil Sight content.** No routines,
no scripts, no performance material. It is reference data and the code that
builds it.

---

## What is published

| Dataset | What it answers | Records | Installed | Download |
| --- | --- | --- | --- | --- |
| `us-zip-cities` | Every US ZIP code → its place, county, state, time zone, and the largest city nearby | 41,488 | 6.3 MB | 1.9 MB |
| `birthday-almanac` | The one per-date fact that cannot be computed (sign, stone, flower, Chinese zodiac and days alive are all computed on device) | 366 | 0.1 MB | 0.04 MB |
| `world-leaders` | Heads of state and government for every country, by year | 2,817 terms / 347 countries | 0.4 MB | 0.12 MB |
| `name-meanings` | First and family name etymology | 50,000 | ~6 MB | ~2 MB |
| `celebrities` | Notable people: birth/death, partner, residence, notable work | 46,612 | 13.3 MB | 5.8 MB |
| `on-this-day` | Notable events for every calendar day, 125 years deep | 108,000 | 49 MB | 17.7 MB |
| `wikipedia-en` | Offline English Wikipedia lead extracts with full-text search | ~6.9 M | multi-GB | multi-GB |

Sizes are from the current build; the manifest is authoritative.

## Licensing

Each dataset carries its upstream licence in its `meta` table and in the
manifest, and the app displays the attribution on its Data Sources screen.

| Source | Licence | Requirement |
| --- | --- | --- |
| GeoNames | CC BY 4.0 | Attribution |
| Wikidata | CC0 1.0 | None |
| Wikipedia | CC BY-SA 4.0 | Attribution + share-alike |
| Wiktionary | CC BY-SA 4.0 | Attribution + share-alike |

The build code in this repository is MIT. The **data** carries its upstream
licence, which is why the derived containers are published here rather than
folded into the app's own licence.

---

## Repository layout

```
manifest/catalog.json     the document the app fetches
builders/                 one script per dataset
tools/build_catalog.py    assembles the manifest from dist/
tools/validate_catalog.py checks the manifest against the app's contract
build/                    generated .sqlite3 containers (gitignored)
dist/                     generated .sqlite3.gz + per-release JSON (published)
.cache/                   downloaded upstream dumps (gitignored)
```

## Building

```bash
python3 -m pip install -r requirements.txt

python3 builders/build_us_zip_cities.py
python3 builders/build_world_leaders.py
python3 builders/build_celebrities.py
python3 builders/build_on_this_day.py
python3 builders/build_birthday_almanac.py     # needs on-this-day first
python3 builders/build_name_meanings.py
python3 builders/build_wikipedia.py --limit 50000   # or omit --limit for the full dump

python3 tools/build_catalog.py
python3 tools/validate_catalog.py
```

Upstream dumps are cached in `.cache/`, so re-running a builder while tuning its
transform does not re-download gigabytes.

Datasets not rebuilt keep their existing published entry, so refreshing one does
not retract the others.

### Where the queries run

`builders/wikidata.py` uses [QLever](https://qlever.dev/api/wikidata) rather than
`query.wikidata.org`. Every query broad enough to be useful here — "all humans
with a birth date", "all heads of government ever" — times out at 60 s on the
official endpoint and returns in seconds on QLever, over the same data.

---

## The container contract

Every dataset is one SQLite file named `<dataset-id>.sqlite3` carrying:

- a `meta` table of key/value strings, including `schema_version`
- the tables listed in `ReferenceDatabase.requiredTables(for:)` on the Swift side

The app verifies **both** halves before an installed dataset replaces a working
one: the SHA-256 of the downloaded bytes, then a structural check that the
container actually has the tables its provider will query. A checksum only
proves the bytes arrived; it says nothing about whether the generator emitted
the right schema.

`SCHEMA_VERSIONS` in `builders/common.py` and
`ReferenceDatasetSchema.supportedVersions` in
`LocalPackages/ReferenceData/Sources/ReferenceDataKit/Manifest/ReferenceDataset.swift`
are the two halves of that contract. Bump both together.

### Key normalisation

Three functions have twins on each side, and a drift between them makes every
lookup miss **silently** — no error, just no result:

| Python (`builders/common.py`) | Swift |
| --- | --- |
| `country_key` | `WorldLeaderProvider.countryKey` |
| `name_key` | `NameMeaningProvider.nameKey` |
| `title_key` | `WikipediaProvider.titleKey` |

The Swift side pins the expected outputs in `KeyTests`.

---

## Manifest shape

```jsonc
{
  "manifestVersion": 1,
  "generatedAt": "2026-08-06T00:00:00Z",
  "datasets": [
    {
      "id": "us-zip-cities",
      "title": "US ZIP Codes & Cities",
      "summary": "…",
      "category": "places",
      "symbolName": "mappin.and.ellipse",
      "attribution": "GeoNames postal codes and cities15000",
      "license": "CC BY 4.0",
      "sourceURL": "https://www.geonames.org/",
      "bundled": true,
      "recordCountEstimate": 41488,
      "release": {
        "version": "2026.08.06",
        "schemaVersion": 1,
        "downloadURL": "https://raw.githubusercontent.com/AM-Guru/SybilSight-DataSources/main/dist/us-zip-cities.sqlite3.gz",
        "downloadBytes": 1943410,
        "installedBytes": 6324224,
        "sha256": "…",
        "compression": "gzip",
        "publishedAt": "2026-08-06T00:00:00Z",
        "releaseNotes": "Rebuilt 2026.08.06."
      }
    }
  ]
}
```

`version` is compared with dotted-numeric ordering, never as a string — `1.10`
is newer than `1.9`, which a string comparison gets backwards.

A dataset whose `bundled` flag is true ships inside the app. It can still be
updated from here: the app prefers a downloaded build over its built-in copy.

---

## Editorial notes

Two decisions in the data are worth knowing about, because they are judgement
calls rather than mechanical transforms.

**"Biggest city" for a ZIP code** is the largest-population city within 80 km,
with a 1.6× preference for the same state, falling back to 250 km and then to
the nearest city at any distance. That last rule is what keeps rural Alaska and
Nevada answerable. `build_us_zip_cities.py` states this at the top.

**`on-this-day` carries a `sensitive` flag** on entries matching a violence
keyword list. Nothing is filtered out — a history question returns the full
answer, as it must — but a birthday reading prefers a non-sensitive entry. The
first ranking attempt weighted recency, which systematically put modern
atrocities at the top of every date; 20 July led with a mass shooting instead of
the moon landing. It is a keyword heuristic and it will miss cases.

---

## Publishing

`dist/` is committed, because the manifest's `downloadURL` points at the raw CDN
for this repository. After a rebuild:

```bash
python3 tools/build_catalog.py
python3 tools/validate_catalog.py
git add dist manifest && git commit -m "Rebuild <dataset>" && git push
```

The app picks the change up on its next catalog refresh (at most every 6 hours,
or immediately on pull-to-refresh in Settings › Data Sources).
