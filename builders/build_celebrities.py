#!/usr/bin/env python3
"""celebrities — the 50k most notable people, with the facts a performer uses.

Source: Wikidata (CC0) for the structured facts, English Wikipedia lead
descriptions (CC BY-SA 4.0) for the one-line "who is this".

"Most notable 50k" needs a defensible ranking, because Wikidata has ~11 million
humans and almost none of them are people a spectator will name. The score used
here is the number of distinct Wikipedia language editions carrying an article
about the person (``sitelinks``). It is the standard proxy in the Wikidata
community, it is language-neutral, and it correlates strongly with "a stranger
in a bar could name them" — which is exactly the population this dataset is for.

Note what is deliberately NOT stored: days alive and age. Both are computed on
device from the birth date, because a stored value is wrong the day after the
dataset is built.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import log, new_container, package, write_meta  # noqa: E402
from wikidata import iso_date, sparql  # noqa: E402

DATASET_ID = "celebrities"
TARGET_COUNT = 50_000
# Wikipedia editions carrying the person. 12+ keeps the set at roughly the
# target size while excluding the long tail of locally-notable officials.
MIN_SITELINKS = 12

CORE_QUERY = """
SELECT ?person ?personLabel ?sitelinks ?birth ?death ?description WHERE {
  ?person wdt:P31 wd:Q5 .
  ?person wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks >= %d)
  ?person wdt:P569 ?birth .
  OPTIONAL { ?person wdt:P570 ?death }
  ?person rdfs:label ?personLabel . FILTER(LANG(?personLabel) = "en")
  OPTIONAL { ?person schema:description ?description . FILTER(LANG(?description) = "en") }
}
ORDER BY DESC(?sitelinks)
LIMIT %d
""" % (MIN_SITELINKS, TARGET_COUNT)

# Attribute queries run separately and are joined in Python. One giant query
# with eight OPTIONAL blocks is quadratic on the endpoint and times out; eight
# narrow queries each return in seconds.
ATTRIBUTE_QUERIES = {
    "occupations": ("P106", "occupation"),
    "citizenship": ("P27", "country of citizenship"),
    "birth_place": ("P19", "place of birth"),
    "residence": ("P551", "residence"),
    "partner": ("P26", "spouse"),
    "unmarried_partner": ("P451", "unmarried partner"),
    "notable_works": ("P800", "notable work"),
    "awards": ("P166", "award received"),
}

ATTRIBUTE_TEMPLATE = """
SELECT ?person ?valueLabel WHERE {
  ?person wdt:P31 wd:Q5 .
  ?person wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks >= %d)
  ?person wdt:%s ?value .
  ?value rdfs:label ?valueLabel . FILTER(LANG(?valueLabel) = "en")
}
"""

HEIGHT_QUERY = """
SELECT ?person ?height WHERE {
  ?person wdt:P31 wd:Q5 .
  ?person wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks >= %d)
  ?person wdt:P2048 ?height .
}
""" % MIN_SITELINKS


def fetch_people() -> dict[str, dict]:
    people: dict[str, dict] = {}
    for row in sparql(CORE_QUERY):
        qid = row.get("person", "")
        label = row.get("personLabel", "").strip()
        if not qid or not label or (label.startswith("Q") and label[1:].isdigit()):
            continue
        birth = iso_date(row.get("birth", ""))
        if not birth or len(birth) < 10:
            # A birth date without a month and day cannot answer "days alive",
            # which is the headline fact of this dataset.
            continue
        try:
            sitelinks = int(row.get("sitelinks", "0"))
        except ValueError:
            sitelinks = 0
        people[qid] = {
            "qid": qid,
            "name": label,
            "description": row.get("description", "").strip(),
            "birth_date": birth,
            "death_date": iso_date(row.get("death", "")) or None,
            "birth_place": "",
            "residence": "",
            "citizenship": "",
            "occupations": "",
            "partner": "",
            "partner_status": "",
            "notable_works": "",
            "awards": "",
            "height": "",
            "notability": sitelinks,
        }
    log(f"{len(people):,} people with a full birth date and >= {MIN_SITELINKS} sitelinks")
    return people


def fetch_attribute(prop: str, wanted: set[str], limit_per_person: int) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {}
    for row in sparql(ATTRIBUTE_TEMPLATE % (MIN_SITELINKS, prop)):
        qid = row.get("person", "")
        value = row.get("valueLabel", "").strip()
        if qid not in wanted or not value:
            continue
        if value.startswith("Q") and value[1:].isdigit():
            continue
        values = collected.setdefault(qid, [])
        if len(values) < limit_per_person and value not in values:
            values.append(value)
    return collected


def build() -> None:
    people = fetch_people()
    if not people:
        raise SystemExit("no people returned — refusing to publish an empty dataset")
    wanted = set(people)

    limits = {
        "occupations": 4,
        "citizenship": 2,
        "birth_place": 1,
        "residence": 1,
        "partner": 2,
        "unmarried_partner": 1,
        "notable_works": 5,
        "awards": 3,
    }
    for field, (prop, human) in ATTRIBUTE_QUERIES.items():
        log(f"querying {human} ({prop})")
        values = fetch_attribute(prop, wanted, limits[field])
        for qid, items in values.items():
            joined = " | ".join(items)
            if field == "unmarried_partner":
                # Only fill from P451 when there is no spouse — "partner" and
                # "spouse" are different relationship statuses and the app
                # labels them differently.
                if not people[qid]["partner"]:
                    people[qid]["partner"] = joined
                    people[qid]["partner_status"] = "Partner"
            elif field == "partner":
                people[qid]["partner"] = joined
                people[qid]["partner_status"] = "Spouse"
            elif field in ("birth_place", "residence", "citizenship"):
                people[qid][field] = items[0] if field == "birth_place" else joined
            else:
                people[qid][field] = joined

    log("querying height (P2048)")
    for row in sparql(HEIGHT_QUERY):
        qid = row.get("person", "")
        if qid not in people:
            continue
        try:
            centimetres = float(row.get("height", "0"))
        except ValueError:
            continue
        if 50 <= centimetres <= 280:
            people[qid]["height"] = f"{centimetres:.0f} cm"

    records = sorted(people.values(), key=lambda p: -p["notability"])

    with new_container(DATASET_ID) as connection:
        connection.executescript(
            """
            CREATE TABLE person (
                qid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                birth_date TEXT,
                death_date TEXT,
                birth_place TEXT,
                residence TEXT,
                citizenship TEXT,
                occupations TEXT,
                partner TEXT,
                partner_status TEXT,
                notable_works TEXT,
                awards TEXT,
                height TEXT,
                notability INTEGER NOT NULL
            );
            CREATE INDEX person_name ON person (name);
            -- external-content FTS: the index stores only the tokenised name,
            -- so it adds a few MB rather than duplicating every row.
            CREATE VIRTUAL TABLE person_fts USING fts5(
                name, content='person', content_rowid='rowid', tokenize='unicode61'
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO person (qid, name, description, birth_date, death_date, birth_place,
                                residence, citizenship, occupations, partner, partner_status,
                                notable_works, awards, height, notability)
            VALUES (:qid, :name, :description, :birth_date, :death_date, :birth_place,
                    :residence, :citizenship, :occupations, :partner, :partner_status,
                    :notable_works, :awards, :height, :notability)
            """,
            records,
        )
        connection.execute(
            "INSERT INTO person_fts (rowid, name) SELECT rowid, name FROM person"
        )
        write_meta(
            connection,
            DATASET_ID,
            record_count=len(records),
            min_sitelinks=MIN_SITELINKS,
            source="Wikidata humans ranked by Wikipedia sitelink count",
            license="CC0 1.0 (facts) / CC BY-SA 4.0 (descriptions)",
        )

    package(DATASET_ID, compression="gzip")


if __name__ == "__main__":
    build()
