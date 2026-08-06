"""Wikidata access for the builders.

QLever (https://qlever.dev/api/wikidata) rather than query.wikidata.org: the
official endpoint times out at 60s on every query broad enough to be useful
here — "all humans with a birth date" and "all heads of government ever" both
die there and both return in seconds on QLever, over the same data.

Both endpoints are kept because QLever indexes a periodic dump; when a query
needs today's data more than it needs to finish, WDQS is the fallback.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

QLEVER_ENDPOINT = "https://qlever.dev/api/wikidata"
WDQS_ENDPOINT = "https://query.wikidata.org/sparql"

USER_AGENT = "SybilSight-DataSources/1.0 (+https://github.com/AM-Guru/SybilSight-DataSources)"

PREFIXES = """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX schema: <http://schema.org/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""


def sparql(query: str, endpoint: str = QLEVER_ENDPOINT, retries: int = 3, timeout: int = 600):
    """Run a query and yield plain dict rows (URIs shortened to Q-ids)."""
    body = urllib.parse.urlencode({"query": PREFIXES + query}).encode()
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            # Plain linear backoff: these endpoints rate-limit by concurrency,
            # not by a token bucket, so waiting longer rarely helps more than
            # waiting a little.
            time.sleep(5 * (attempt + 1))
    else:
        raise SystemExit(f"SPARQL failed after {retries} attempts: {last_error}")

    for binding in payload["results"]["bindings"]:
        row = {}
        for key, cell in binding.items():
            value = cell.get("value", "")
            if cell.get("type") == "uri" and "/entity/Q" in value:
                value = value.rsplit("/", 1)[-1]
            row[key] = value
        yield row


def year_of(iso_value: str) -> int | None:
    """Wikidata dates arrive as ``+1961-01-20T00:00:00Z`` and, for BCE or
    imprecise values, as things like ``-0044-03-15T00:00:00Z``. Only CE years
    are useful to these datasets."""
    if not iso_value:
        return None
    text = iso_value.lstrip("+")
    if text.startswith("-"):
        return None
    try:
        year = int(text[:4])
    except ValueError:
        return None
    return year if 1 <= year <= 2200 else None


def iso_date(value: str) -> str | None:
    """Normalise to YYYY-MM-DD, dropping the placeholder 00 month/day Wikidata
    uses for year- and month-precision values."""
    if not value:
        return None
    text = value.lstrip("+")
    if text.startswith("-") or len(text) < 4:
        return None
    year = text[:4]
    month = text[5:7] if len(text) >= 7 else "00"
    day = text[8:10] if len(text) >= 10 else "00"
    if month == "00":
        return year
    if day == "00":
        return f"{year}-{month}"
    return f"{year}-{month}-{day}"
