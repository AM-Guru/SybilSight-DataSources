"""Shared helpers for every dataset builder.

Contract with the app (LocalPackages/ReferenceData):

* Every dataset is ONE SQLite container named ``<dataset-id>.sqlite3``.
* Every container carries a ``meta`` table of key/value strings, including
  ``schema_version`` — ``ReferenceDatasetSchema.supportedVersions`` in Swift is
  the other half of that contract, and a mismatch makes the app refuse the file
  rather than mis-read its columns.
* The tables a container must expose are listed in
  ``ReferenceDatabase.requiredTables(for:)``. Dropping one here fails the app's
  install-time structural check, which is the point: it fails at install rather
  than mid-performance.
"""

from __future__ import annotations

import contextlib
import hashlib
import gzip
import json
import math
import os
import shutil
import sqlite3
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO_ROOT / "build"
CACHE_DIR = REPO_ROOT / ".cache"
DIST_DIR = REPO_ROOT / "dist"

USER_AGENT = "SybilSight-DataSources/1.0 (+https://github.com/AM-Guru/SybilSight-DataSources)"

# Bumped per dataset when its table layout changes. Must stay in step with
# ReferenceDatasetSchema.supportedVersions on the Swift side.
SCHEMA_VERSIONS = {
    "us-zip-cities": 1,
    "celebrities": 1,
    "on-this-day": 1,
    "name-meanings": 1,
    "world-leaders": 1,
    "birthday-almanac": 1,
    "wikipedia-en": 1,
    "constant-digits": 1,
}


def build_stamp() -> str:
    """Date-ordered version, matching ReferenceDataVersion's dotted-numeric
    comparison (which is why this is 2026.08.06 and not 2026-08-06)."""
    return datetime.now(timezone.utc).strftime("%Y.%m.%d")


def ensure_dirs() -> None:
    for directory in (BUILD_DIR, CACHE_DIR, DIST_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    print(f"[build] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- downloads


def download(url: str, filename: str | None = None, force: bool = False) -> Path:
    """Fetch to the on-disk cache. Builders are re-run often while their
    transforms are tuned; re-pulling a 600 MB dump each time is pure waste."""
    ensure_dirs()
    name = filename or url.rstrip("/").split("/")[-1]
    target = CACHE_DIR / name
    if target.exists() and not force:
        log(f"cached {name} ({target.stat().st_size:,} bytes)")
        return target
    log(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temporary = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(request, timeout=300) as response, open(temporary, "wb") as handle:
        shutil.copyfileobj(response, handle, length=1 << 20)
    temporary.replace(target)
    log(f"downloaded {name} ({target.stat().st_size:,} bytes)")
    return target


def unzip_member(archive: Path, member: str) -> Path:
    target = CACHE_DIR / member
    if target.exists():
        return target
    with zipfile.ZipFile(archive) as zf:
        zf.extract(member, CACHE_DIR)
    return target


# ---------------------------------------------------------------- SQLite


@contextlib.contextmanager
def new_container(dataset_id: str):
    """Create a fresh container, yield the connection, then compact it.

    VACUUM + a large page size matter here: these files are memory-mapped and
    read-only on device, so a compacted container with sequential pages is both
    smaller to ship and cheaper to page in.
    """
    ensure_dirs()
    path = BUILD_DIR / f"{dataset_id}.sqlite3"
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA page_size = 8192;
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    try:
        yield connection
        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()
    log(f"built {path.name} ({path.stat().st_size:,} bytes)")


def write_meta(connection: sqlite3.Connection, dataset_id: str, **extra: object) -> None:
    rows = {
        "dataset_id": dataset_id,
        "schema_version": str(SCHEMA_VERSIONS[dataset_id]),
        "version": build_stamp(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    rows.update({key: str(value) for key, value in extra.items()})
    connection.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", sorted(rows.items())
    )


# ---------------------------------------------------------------- keys

# These three MUST mirror their Swift counterparts exactly
# (WorldLeaderProvider.countryKey, NameMeaningProvider.nameKey,
# WikipediaProvider.titleKey). A drift here produces a dataset whose keys the
# app can never match — and it fails as "no result", not as an error, which is
# the hardest kind of bug to notice.

import unicodedata
import re


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def country_key(value: str) -> str:
    text = _fold(value).strip()
    if text.startswith("the "):
        text = text[4:]
    parts = [p for p in re.split(r"[^a-z0-9]+", text) if p]
    return "-".join(parts)


def name_key(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", _fold(value)))


def title_key(value: str) -> str:
    parts = [p for p in re.split(r"[^a-z0-9]+", _fold(value)) if p]
    return "_".join(parts)


# ---------------------------------------------------------------- geo


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- packaging


@dataclass
class PackagedRelease:
    dataset_id: str
    version: str
    schema_version: int
    download_bytes: int
    installed_bytes: int
    sha256: str
    compression: str
    path: Path


def package(dataset_id: str, compression: str = "gzip") -> PackagedRelease:
    """Compress a built container and hash it EXACTLY as it will be downloaded.

    The app verifies the compressed bytes before it spends any CPU expanding
    them, so the published hash has to be of the transfer artefact, not of the
    expanded database.
    """
    ensure_dirs()
    source = BUILD_DIR / f"{dataset_id}.sqlite3"
    if not source.exists():
        raise SystemExit(f"{source} does not exist — run its builder first")
    installed_bytes = source.stat().st_size

    if compression == "none":
        target = DIST_DIR / source.name
        shutil.copy2(source, target)
    elif compression == "gzip":
        target = DIST_DIR / f"{source.name}.gz"
        # mtime=0 so an unchanged database produces byte-identical output and
        # its hash (and therefore the manifest) does not churn on every run.
        with open(source, "rb") as raw, gzip.GzipFile(target, "wb", compresslevel=9, mtime=0) as gz:
            shutil.copyfileobj(raw, gz, length=1 << 20)
    else:
        raise SystemExit(f"unsupported compression: {compression}")

    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)

    release = PackagedRelease(
        dataset_id=dataset_id,
        version=build_stamp(),
        schema_version=SCHEMA_VERSIONS[dataset_id],
        download_bytes=target.stat().st_size,
        installed_bytes=installed_bytes,
        sha256=digest.hexdigest(),
        compression=compression,
        path=target,
    )
    log(
        f"packaged {dataset_id}: {release.download_bytes:,} transfer / "
        f"{release.installed_bytes:,} installed / sha256 {release.sha256[:16]}…"
    )
    (DIST_DIR / f"{dataset_id}.release.json").write_text(
        json.dumps(
            {
                "datasetId": release.dataset_id,
                "version": release.version,
                "schemaVersion": release.schema_version,
                "downloadBytes": release.download_bytes,
                "installedBytes": release.installed_bytes,
                "sha256": release.sha256,
                "compression": release.compression,
                "fileName": release.path.name,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return release
