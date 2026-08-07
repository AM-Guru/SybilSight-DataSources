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

# Mirrors ReferenceDatasetDescriptor.bundleCeilingBytes and GitHub's own limits.
BUNDLE_CEILING = 20 * 1000 * 1000
GITHUB_BLOB_LIMIT = 100 * 1000 * 1000
GITHUB_ASSET_LIMIT = 2 * 1000 * 1000 * 1000

# Mirrors ReferenceDatabase.requiredTables(for:). Keep in step.
REQUIRED_TABLES = {
    "us-zip-cities": {"meta", "zip"},
    "celebrities": {"meta", "person", "person_fts"},
    "on-this-day": {"meta", "event"},
    "name-meanings": {"meta", "name"},
    "world-leaders": {"meta", "leader", "country_alias"},
    "birthday-almanac": {"meta", "almanac"},
    "wikipedia-en": {"meta", "article", "article_fts"},
    "constant-digits": {"meta", "constant", "digit_chunk"},
    "constant-digits-7": {"meta", "constant", "digit_chunk"},
    "constant-digits-8": {"meta", "constant", "digit_chunk"},
    "constant-digits-9": {"meta", "constant", "digit_chunk"},
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

    # A dataset over the ceiling that still claims to be bundled would send the
    # app hunting inside its own binary for a file that is not there.
    if entry.get("bundled") and release.get("installedBytes", 0) > BUNDLE_CEILING:
        fail(
            f"{dataset_id}: bundled=true but {release['installedBytes']:,} bytes installed "
            f"is over the {BUNDLE_CEILING:,} ceiling"
        )

    # Every part must exist and hash to the manifest value, or that install
    # fails integrity verification on device.
    parts = release.get("parts") or [{
        "index": 0,
        "fileName": url.rsplit("/", 1)[-1],
        "downloadURL": url,
        "downloadBytes": release.get("downloadBytes", 0),
        "sha256": release.get("sha256", ""),
    }]

    seen_indices = set()
    total = 0
    for part in parts:
        index = part.get("index", 0)
        if index in seen_indices:
            fail(f"{dataset_id}: duplicate part index {index}")
        seen_indices.add(index)

        part_url = str(part.get("downloadURL", ""))
        size = part.get("downloadBytes", 0)
        if size > GITHUB_ASSET_LIMIT:
            fail(f"{dataset_id}: part {index} is {size:,} bytes, over GitHub's 2 GB asset limit")
        # Anything over the blob limit cannot be served from raw — it cannot be
        # committed at all. It has to be a Release asset.
        if size >= GITHUB_BLOB_LIMIT and "raw.githubusercontent.com" in part_url:
            fail(
                f"{dataset_id}: part {index} is {size:,} bytes but is published from raw, "
                f"which caps at {GITHUB_BLOB_LIMIT:,} — use a Release asset"
            )
        total += size

        artefact = DIST / part["fileName"]
        if not artefact.exists():
            fail(f"{dataset_id}: {artefact.relative_to(REPO_ROOT)} is not in dist/")
            continue
        if artefact.stat().st_size != size:
            fail(
                f"{dataset_id}: part {index} downloadBytes is {size} but "
                f"{part['fileName']} is {artefact.stat().st_size} bytes"
            )
        digest = hashlib.sha256()
        with open(artefact, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 22), b""):
                digest.update(chunk)
        if digest.hexdigest() != str(part.get("sha256", "")).lower():
            fail(f"{dataset_id}: sha256 does not match {part['fileName']}")

    # Parts must be contiguous from 0, since they install in order and a hole
    # would leave the reader searching across a gap.
    if seen_indices and seen_indices != set(range(len(parts))):
        fail(f"{dataset_id}: part indices {sorted(seen_indices)} are not 0..{len(parts) - 1}")
    if len(parts) > 1 and total != release["downloadBytes"]:
        fail(
            f"{dataset_id}: parts total {total:,} bytes but downloadBytes says "
            f"{release['downloadBytes']:,}"
        )


def check_container(dataset_id: str) -> None:
    # Multi-part datasets have no single container. Checking only the plain name
    # made the structural verification SKIP them entirely — the largest and most
    # expensive sets got the least checking, which is backwards.
    containers = [BUILD / f"{dataset_id}.sqlite3"]
    if not containers[0].exists():
        containers = sorted(BUILD.glob(f"{dataset_id}.part*.sqlite3"))
    if not containers:
        notes.append(f"{dataset_id}: no local container to structurally verify (not rebuilt here)")
        return
    for container in containers:
        check_one_container(dataset_id, container)
    if len(containers) > 1:
        check_part_continuity(dataset_id, containers)


def check_part_continuity(dataset_id: str, containers: list[Path]) -> None:
    """Parts must tile the decimal places with no gap and no overlap.

    A hole here would make the reader stop early and report a shallower search
    than it ran; an overlap would shift every position after the seam.
    """
    spans: dict[str, list[tuple[int, int]]] = {}
    for container in containers:
        connection = sqlite3.connect(f"file:{container}?mode=ro", uri=True)
        try:
            for name, start, count in connection.execute(
                "SELECT name, part_start, part_digits FROM constant"
            ):
                spans.setdefault(name, []).append((start, count))
        except sqlite3.Error as error:
            fail(f"{dataset_id}: {container.name} has no readable constant table ({error})")
        finally:
            connection.close()

    for name, ranges in spans.items():
        expected = 1
        for start, count in sorted(ranges):
            if start != expected:
                fail(
                    f"{dataset_id}: {name} parts are not contiguous — expected place "
                    f"{expected:,} next, got {start:,}"
                )
                break
            expected += count


def check_one_container(dataset_id: str, container: Path) -> None:
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
            fail(f"{container.name}: missing table(s) {sorted(missing)}")
        row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row:
            fail(f"{container.name}: no schema_version in meta")
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
