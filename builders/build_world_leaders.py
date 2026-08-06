#!/usr/bin/env python3
"""world-leaders — heads of state and heads of government, by country and year.

Source: Wikidata P35 (head of state) and P6 (head of government) statements
with their P580/P582 term qualifiers. CC0.

Rows are stored as spans, not as year-by-year rows, so the app's
``start_year <= Y AND (end_year IS NULL OR end_year >= Y)`` predicate naturally
returns EVERY officeholder in a transition year. That is the behaviour the
existing US-only ``PresidentOfYearLookup`` already established (1963 answers
Kennedy and Johnson) and the reason this dataset does not collapse a year to a
single name.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import country_key, log, new_container, package, write_meta  # noqa: E402
from wikidata import sparql, year_of  # noqa: E402

DATASET_ID = "world-leaders"

# Sovereign states, plus historical countries so a 1935 question about the
# German Reich or the USSR resolves instead of returning nothing.
COUNTRY_CLASSES = ["wd:Q6256", "wd:Q3624078", "wd:Q3024240"]

QUERY_TEMPLATE = """
SELECT ?country ?countryLabel ?person ?personLabel ?start ?end WHERE {{
  ?country wdt:P31 {klass} .
  ?country p:{property} ?statement .
  ?statement ps:{property} ?person .
  OPTIONAL {{ ?statement pq:P580 ?start }}
  OPTIONAL {{ ?statement pq:P582 ?end }}
  ?country rdfs:label ?countryLabel . FILTER(LANG(?countryLabel) = "en")
  ?person rdfs:label ?personLabel . FILTER(LANG(?personLabel) = "en")
}}
"""

ALIAS_QUERY_TEMPLATE = """
SELECT ?country ?countryLabel ?alias WHERE {{
  ?country wdt:P31 {klass} .
  ?country rdfs:label ?countryLabel . FILTER(LANG(?countryLabel) = "en")
  ?country skos:altLabel ?alias . FILTER(LANG(?alias) = "en")
}}
"""

ROLE_BY_PROPERTY = {"P6": "head of government", "P35": "head of state"}

# Colloquial names people actually say, which Wikidata's altLabels do not
# always carry. Kept explicit and small rather than inferred.
MANUAL_ALIASES = {
    "united-states": ["usa", "us", "america", "united states of america", "the states"],
    "united-kingdom": ["uk", "britain", "great britain", "england"],
    "netherlands": ["holland"],
    "russia": ["russian federation"],
    "south-korea": ["korea", "republic of korea"],
    "north-korea": ["dprk"],
    "china": ["prc", "peoples republic of china"],
    "ireland": ["republic of ireland", "eire"],
    "czech-republic": ["czechia"],
    "myanmar": ["burma"],
    "turkey": ["turkiye"],
    "soviet-union": ["ussr", "the soviet union"],
}


def fetch_terms() -> list[dict]:
    records: dict[tuple, dict] = {}
    for prop, role in ROLE_BY_PROPERTY.items():
        for klass in COUNTRY_CLASSES:
            log(f"querying {role} for {klass}")
            for row in sparql(QUERY_TEMPLATE.format(klass=klass, property=prop)):
                country = row.get("countryLabel", "").strip()
                person = row.get("personLabel", "").strip()
                if not country or not person:
                    continue
                # Skip the Q-id placeholders Wikidata emits for unlabelled items.
                if person.startswith("Q") and person[1:].isdigit():
                    continue
                start = year_of(row.get("start", ""))
                end = year_of(row.get("end", ""))
                if start is None:
                    # A term with no start year cannot answer a year question,
                    # and guessing one would silently invent history.
                    continue
                if end is not None and end < start:
                    continue
                key = (country_key(country), person, role, start)
                existing = records.get(key)
                if existing and (existing["end_year"] or 0) >= (end or 9999):
                    continue
                records[key] = {
                    "country_key": country_key(country),
                    "country": country,
                    "name": person,
                    "role": role,
                    "start_year": start,
                    "end_year": end,
                    "party": "",
                    "note": "",
                }
    log(f"{len(records):,} leadership terms")
    return list(records.values())


def fetch_aliases(valid_keys: set[str]) -> list[dict]:
    aliases: dict[tuple, dict] = {}
    for klass in COUNTRY_CLASSES:
        for row in sparql(ALIAS_QUERY_TEMPLATE.format(klass=klass)):
            country = row.get("countryLabel", "").strip()
            alias = row.get("alias", "").strip()
            if not country or not alias:
                continue
            key = country_key(country)
            if key not in valid_keys:
                continue
            alias_k = country_key(alias)
            if not alias_k or alias_k == key:
                continue
            aliases[(alias_k, key)] = {"alias_key": alias_k, "country_key": key}

    for key, extra in MANUAL_ALIASES.items():
        if key not in valid_keys:
            continue
        for alias in extra:
            alias_k = country_key(alias)
            aliases[(alias_k, key)] = {"alias_key": alias_k, "country_key": key}

    log(f"{len(aliases):,} country aliases")
    return list(aliases.values())


def build() -> None:
    terms = fetch_terms()
    if not terms:
        raise SystemExit("no leadership terms returned — refusing to publish an empty dataset")
    valid_keys = {t["country_key"] for t in terms}
    aliases = fetch_aliases(valid_keys)

    with new_container(DATASET_ID) as connection:
        connection.executescript(
            """
            CREATE TABLE leader (
                country_key TEXT NOT NULL,
                country TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                start_year INTEGER NOT NULL,
                end_year INTEGER,
                party TEXT,
                note TEXT
            );
            CREATE INDEX leader_lookup ON leader (country_key, start_year, end_year);
            CREATE TABLE country_alias (
                alias_key TEXT NOT NULL,
                country_key TEXT NOT NULL,
                PRIMARY KEY (alias_key, country_key)
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO leader (country_key, country, name, role, start_year, end_year, party, note)
            VALUES (:country_key, :country, :name, :role, :start_year, :end_year, :party, :note)
            """,
            terms,
        )
        connection.executemany(
            "INSERT OR IGNORE INTO country_alias (alias_key, country_key) VALUES (:alias_key, :country_key)",
            aliases,
        )
        write_meta(
            connection,
            DATASET_ID,
            record_count=len(terms),
            country_count=len(valid_keys),
            source="Wikidata P35 / P6 with P580 / P582 qualifiers",
            license="CC0 1.0",
        )

    package(DATASET_ID, compression="gzip")


if __name__ == "__main__":
    build()
