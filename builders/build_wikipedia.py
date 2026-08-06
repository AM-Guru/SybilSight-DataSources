#!/usr/bin/env python3
"""wikipedia-en — offline English Wikipedia lead extracts with full-text search.

Source: the enwiki abstracts dump (CC BY-SA 4.0), which carries the title and
opening paragraph of every article — about 6.9 million of them.

Lead extracts rather than full article text is a deliberate trade. Full text is
~90 GB and unusable on a phone; the lead paragraph is what actually answers
"who was X" / "what is Y", it is what the AI Listener needs to ground a lookup,
and the whole thing fits in a few gigabytes with a searchable index.

Usage:
    python3 builders/build_wikipedia.py                # full dump
    python3 builders/build_wikipedia.py --limit 50000  # smaller popular subset

``--limit`` keeps the N longest extracts, which correlates well with article
maturity and therefore with notability. It exists so the pipeline can be
verified end to end without a multi-gigabyte build, and so a "Wikipedia
(compact)" release can be published for users who will not spend the space.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import download, log, new_container, package, title_key, write_meta  # noqa: E402

DATASET_ID = "wikipedia-en"
ABSTRACTS_URL = "https://dumps.wikimedia.org/enwiki/latest/enwiki-latest-abstract.xml.gz"

# Below this an "abstract" is a stub sentence fragment or a disambiguation
# pointer, neither of which answers a question.
MIN_EXTRACT_CHARS = 80
TITLE_PREFIX = "Wikipedia: "


def iter_articles(path: Path):
    """Stream the dump. It does not fit in memory, and `ET.parse` on a 6 GB
    document is an immediate OOM even on a workstation."""
    with gzip.open(path, "rb") as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if not element.tag.endswith("doc"):
                continue
            title = (element.findtext("title") or "").strip()
            abstract = (element.findtext("abstract") or "").strip()
            url = (element.findtext("url") or "").strip()
            element.clear()

            if title.startswith(TITLE_PREFIX):
                title = title[len(TITLE_PREFIX):]
            if not title or len(abstract) < MIN_EXTRACT_CHARS:
                continue
            # The dump leaves a few wiki artefacts in the abstract text.
            abstract = re.sub(r"\s+", " ", abstract)
            abstract = abstract.replace("|", " ").strip()
            if abstract.startswith("may refer to") or " may refer to:" in abstract[:60]:
                continue
            yield title, abstract, url


def build(limit: int | None) -> None:
    dump = download(ABSTRACTS_URL, "enwiki-latest-abstract.xml.gz")

    with new_container(DATASET_ID) as connection:
        connection.executescript(
            """
            CREATE TABLE article (
                title TEXT NOT NULL,
                title_key TEXT NOT NULL,
                extract TEXT NOT NULL,
                url TEXT
            );
            """
        )
        cursor = connection.cursor()
        seen: set[str] = set()
        kept = 0
        scanned = 0
        batch = []

        for title, abstract, url in iter_articles(dump):
            scanned += 1
            key = title_key(title)
            if not key or key in seen:
                continue
            seen.add(key)
            batch.append((title, key, abstract, url))
            kept += 1
            if len(batch) >= 20_000:
                cursor.executemany(
                    "INSERT INTO article (title, title_key, extract, url) VALUES (?, ?, ?, ?)",
                    batch,
                )
                batch.clear()
                log(f"  {kept:,} kept / {scanned:,} scanned")
        if batch:
            cursor.executemany(
                "INSERT INTO article (title, title_key, extract, url) VALUES (?, ?, ?, ?)",
                batch,
            )
        connection.commit()

        if limit is not None and kept > limit:
            log(f"trimming to the {limit:,} longest extracts")
            cursor.execute(
                """
                DELETE FROM article WHERE rowid NOT IN (
                    SELECT rowid FROM article ORDER BY LENGTH(extract) DESC LIMIT ?
                )
                """,
                (limit,),
            )
            connection.commit()
            kept = cursor.execute("SELECT COUNT(*) FROM article").fetchone()[0]

        log("building the index")
        connection.executescript(
            """
            CREATE UNIQUE INDEX article_title_key ON article (title_key);
            CREATE VIRTUAL TABLE article_fts USING fts5(
                title, extract, content='article', content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        connection.execute(
            "INSERT INTO article_fts (rowid, title, extract) SELECT rowid, title, extract FROM article"
        )
        connection.execute("INSERT INTO article_fts (article_fts) VALUES ('optimize')")
        write_meta(
            connection,
            DATASET_ID,
            record_count=kept,
            variant="compact" if limit else "full",
            source="English Wikipedia abstracts dump",
            license="CC BY-SA 4.0",
        )
        log(f"{kept:,} articles indexed")

    package(DATASET_ID, compression="gzip")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="keep only the N longest extracts")
    build(parser.parse_args().limit)
