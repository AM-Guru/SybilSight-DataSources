#!/usr/bin/env python3
"""Check manifest/catalog.json against the contract the app decodes.

Run before publishing. A manifest the app cannot decode makes the Data Sources
screen fall back to its bundled copy silently — no error, just a stale list —
which is exactly the failure this catches at the point it is introduced.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "manifest" / "catalog.json"
DIST = REPO_ROOT / "dist"
BUILD = REPO_ROOT / "build"

REQUIRED_DATASET_FIELDS = [
    "id", "title", "summary", "category", "attribution", "license",
    "bundled", "recordCountEstimate", "release",
]
REQUIRED_RELEASE_FIELDS = [
    "version", "schemaVersion", "downloadURL", "downloadBytes",
    "installedBytes", "sha256", "compression", "publishedAt",
]
VALID_CATEGORIES = {"places", "people", "calendar", "language", "encyclopedia"}
VALID_COMPRESSION = {"none", "gzip", "lzfse"}

# Mirrors ReferenceDatabase.requiredTables(for:). Keep in step.
REQUIRED_TABLES = {
    "us-zip-cities": {"meta", "zip"},
    "celebrities": {"meta", "person", "person_fts"},
    "on-this-day": {"meta", "event"},
    "name-meanings": {"meta", "name"},
    "world-leaders": {"meta", "leader", "country_alias"},
    "birthday-almanac": {"meta", "almanac"},
    "wikipedia-en": {"meta", "article", "article_fts"},
}

problems: list[str] = []
notes: list[str] = []


def fail(message: str) -> None:
    problems.append(message)


def check_manifest() -> dict:
    if not MANIFEST.exists():
        raise SystemExit(f"{MANIFEST} does not exist — run tools/build_catalog.py")
    try:
        document = json.loads(MANIFEST.read_text())
    except json.JSONDecodeError as error:
        raise SystemExit(f"catalog.json is not valid JSON: {error}") from error

    if document.get("manifestVersion") != 1:
        fail(f"manifestVersion is {document.get('manifestVersion')}, expected 1")
    if not document.get("datasets"):
        fail("catalog lists no datasets")
    return document


def check_dataset(entry: dict) -> None:
    dataset_id = entry.get("id", "<missing id>")
    for field in REQUIRED_DATASET_FIELDS:
        if field not in entry:
            fail(f"{dataset_id}: missing '{field}'")
    if entry.get("category") not in VALID_CATEGORIES:
        fail(f"{dataset_id}: category '{entry.get('category')}' is not one the app decodes")

    release = entry.get("release", {})
    for field in REQUIRED_RELEASE_FIELDS:
        if field not in release:
            fail(f"{dataset_id}: release is missing '{field}'")
    if release.get("compression") not in VALID_COMPRESSION:
        fail(f"{dataset_id}: compression '{release.get('compression')}' is unsupported")
    if not str(release.get("sha256", "")).strip():
        fail(f"{dataset_id}: empty sha256")
    for size_field in ("downloadBytes", "installedBytes"):
        if not isinstance(release.get(size_field), int) or release[size_field] <= 0:
            fail(f"{dataset_id}: {size_field} must be a positive integer")

    # Version must be dotted-numeric, because that is how the app compares it.
    version = str(release.get("version", ""))
    if not version or not all(part.isdigit() for part in version.split(".") if part):
        fail(f"{dataset_id}: version '{version}' is not dotted-numeric")

    url = str(release.get("downloadURL", ""))
    if not url.startswith("https://"):
        fail(f"{dataset_id}: downloadURL must be https")

    # The published artefact must actually exist and hash to the manifest value,
    # or every install of this dataset fails integrity verification on device.
    filename = url.rsplit("/", 1)[-1]
    artefact = DIST / filename
    if not artefact.exists():
        fail(f"{dataset_id}: {artefact.relative_to(REPO_ROOT)} is not in dist/")
        return
    if artefact.stat().st_size != release["downloadBytes"]:
        fail(
            f"{dataset_id}: downloadBytes is {release['downloadBytes']} but "
            f"{filename} is {artefact.stat().st_size} bytes"
        )
    digest = hashlib.sha256()
    with open(artefact, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != release["sha256"].lower():
        fail(f"{dataset_id}: sha256 does not match {filename}")


def check_container(dataset_id: str) -> None:
    container = BUILD / f"{dataset_id}.sqlite3"
    if not container.exists():
        notes.append(f"{dataset_id}: no local container to structurally verify (not rebuilt here)")
        return
    connection = sqlite3.connect(f"file:{container}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        missing = REQUIRED_TABLES.get(dataset_id, {"meta"}) - tables
        if missing:
            fail(f"{dataset_id}: container is missing table(s) {sorted(missing)}")
        row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row:
            fail(f"{dataset_id}: container has no schema_version in meta")
    finally:
        connection.close()


def main() -> int:
    document = check_manifest()
    for entry in document.get("datasets", []):
        check_dataset(entry)
        if entry.get("id"):
            check_container(entry["id"])

    for note in notes:
        print(f"note: {note}")
    if problems:
        print()
        for problem in problems:
            print(f"FAIL: {problem}")
        print(f"\n{len(problems)} problem(s).")
        return 1
    print(f"\nOK — {len(document.get('datasets', []))} dataset(s) validated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
