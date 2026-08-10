#!/usr/bin/env python3
"""Refresh SybilSight's pointers to official Kiwix/OpenZIM archives.

The generated manifest contains URLs and checksums only. ZIM bytes remain on
Kiwix's official download service and are fetched directly by the user.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parent.parent
SELECTION_PATH = REPO_ROOT / "manifest" / "kiwix-selection.json"
POINTERS_PATH = REPO_ROOT / "manifest" / "kiwix-archives.json"
CACHE_REPORT_PATH = REPO_ROOT / ".cache" / "kiwix-sync-report.json"

ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/terms/"
METALINK = "urn:ietf:params:xml:ns:metalink"
NS = {"atom": ATOM, "dc": DC, "meta": METALINK}

CATALOG_HOST = "library.kiwix.org"
METADATA_HOSTS = {"lb.download.kiwix.org", "download.kiwix.org"}
DOWNLOAD_HOST = "download.kiwix.org"
USER_AGENT = "SybilSight-DataSources/1.0 (+https://github.com/AM-Guru/SybilSight-DataSources)"


def fetch(url: str) -> bytes:
    parsed = urlparse(url)
    allowed = {CATALOG_HOST, *METADATA_HOSTS}
    if parsed.scheme != "https" or parsed.hostname not in allowed:
        raise ValueError(f"refusing non-Kiwix URL: {url}")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    with urlopen(request, timeout=90) as response:
        return response.read()


def text(entry: ET.Element, name: str) -> str:
    return (entry.findtext(f"atom:{name}", default="", namespaces=NS) or "").strip()


def acquisition(entry: ET.Element) -> tuple[str, int]:
    for link in entry.findall("atom:link", NS):
        if link.get("type") == "application/x-zim":
            return link.get("href", ""), int(link.get("length", "0"))
    raise ValueError(f"{text(entry, 'name')} has no ZIM acquisition link")


def metadata(meta4_url: str) -> tuple[str, int, str]:
    parsed = urlparse(meta4_url)
    if parsed.hostname not in METADATA_HOSTS or not parsed.path.startswith("/zim/"):
        raise ValueError(f"unexpected Kiwix Metalink URL: {meta4_url}")
    root = ET.fromstring(fetch(meta4_url))
    file_node = root.find("meta:file", NS)
    if file_node is None:
        raise ValueError(f"Metalink has no file: {meta4_url}")
    size = int(file_node.findtext("meta:size", default="0", namespaces=NS))
    digest = ""
    for node in file_node.findall("meta:hash", NS):
        if node.get("type", "").lower() == "sha-256":
            digest = (node.text or "").strip().lower()
            break
    if len(digest) != 64:
        raise ValueError(f"Metalink has no SHA-256: {meta4_url}")

    path = parsed.path.removesuffix(".meta4")
    direct = urlunparse(("https", DOWNLOAD_HOST, path, "", "", ""))
    return direct, size, digest


def version_from(updated: str) -> str:
    value = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    return f"{value.year:04d}.{value.month:02d}.{value.day:02d}"


def stable_id(name: str, flavor: str) -> str:
    return "kiwix-" + "-".join(filter(None, [name.replace("_", "-"), flavor]))


def flavor_label(flavor: str) -> str:
    return {"mini": "Mini"}.get(flavor, flavor.title() or "Standard")


def load_entries(catalog_url: str, language: str, project: str) -> list[ET.Element]:
    query = urlencode({"lang": language, "category": project, "count": -1})
    root = ET.fromstring(fetch(f"{catalog_url}?{query}"))
    return root.findall("atom:entry", NS)


def build_descriptor(selection: dict, language: str, entry: ET.Element) -> dict:
    name = text(entry, "name")
    flavor = text(entry, "flavour")
    updated = text(entry, "updated")
    meta4_url, advertised_size = acquisition(entry)
    direct_url, exact_size, digest = metadata(meta4_url)
    if advertised_size and advertised_size != exact_size:
        # OPDS length has historically included small transport/container
        # differences. Metalink describes the actual .zim file and is the
        # checksum authority used by the downloader.
        print(
            f"  note: {name}/{flavor} OPDS length {advertised_size:,} != "
            f"Metalink size {exact_size:,}; using Metalink",
            file=sys.stderr,
        )

    browser_url = next(
        (
            link.get("href")
            for link in entry.findall("atom:link", NS)
            if link.get("type") == "text/html" and link.get("href")
        ),
        "https://library.kiwix.org/",
    )
    author = text(entry.find("atom:author", NS) or ET.Element("author"), "name")
    if not author:
        author = selection["title"].split(" (")[0]
    summary = text(entry, "summary")
    title = (
        selection["title"]
        if flavor == "nopic"
        else f"{selection['title']} — {flavor_label(flavor)}"
    )
    upstream_id = text(entry, "id").removeprefix("urn:uuid:")
    article_count = int(text(entry, "articleCount") or 0)

    return {
        "id": stable_id(name, flavor),
        "title": title,
        "summary": f"{summary} Official Kiwix ZIM archive stored for offline knowledge use.",
        "category": "encyclopedia",
        "symbolName": "books.vertical.fill",
        "attribution": f"{author}; packaged by openZIM and distributed by Kiwix",
        "license": "Upstream Wikimedia content and media licenses; see archive metadata",
        "sourceURL": browser_url,
        "bundled": False,
        "recordCountEstimate": article_count,
        "storageKind": "zim",
        "externalArchive": {
            "provider": "Kiwix/openZIM",
            "upstreamID": upstream_id,
            "catalogURL": "https://library.kiwix.org/",
            "metadataURL": meta4_url,
            "language": language,
            "project": selection["project"],
            "flavor": flavor,
            "upstreamUpdatedAt": updated,
        },
        "release": {
            "version": version_from(updated),
            "schemaVersion": 1,
            "downloadURL": direct_url,
            "downloadBytes": exact_size,
            "installedBytes": exact_size,
            "sha256": digest,
            "compression": "none",
            "publishedAt": updated,
            "releaseNotes": f"Kiwix upstream archive updated {updated[:10]}.",
            "parts": [],
        },
    }


def sync(selection_path: Path = SELECTION_PATH) -> tuple[list[dict], list[dict]]:
    config = json.loads(selection_path.read_text())
    language = config.get("language", "eng")
    catalog_url = config.get("catalogURL", "")
    if urlparse(catalog_url).hostname != CATALOG_HOST:
        raise ValueError("catalogURL must use the official library.kiwix.org host")

    by_project: dict[str, list[ET.Element]] = {}
    descriptors: list[dict] = []
    for selected in config["archives"]:
        project = selected["project"]
        entries = by_project.setdefault(
            project, load_entries(catalog_url, language, project)
        )
        for flavor in selected["flavors"]:
            candidates = [
                entry
                for entry in entries
                if text(entry, "name") == selected["name"]
                and text(entry, "flavour") == flavor
            ]
            if not candidates:
                raise ValueError(f"Kiwix catalog has no {selected['name']}/{flavor}")
            latest = max(candidates, key=lambda entry: text(entry, "updated"))
            descriptor = build_descriptor(selected, language, latest)
            descriptors.append(descriptor)
            print(
                f"  {descriptor['id']}: {descriptor['release']['version']} "
                f"{descriptor['release']['downloadBytes']:,} bytes"
            )

    descriptors.sort(key=lambda item: (item["externalArchive"]["project"], item["title"]))
    old_document = json.loads(POINTERS_PATH.read_text()) if POINTERS_PATH.exists() else {"datasets": []}
    old_by_id = {item["id"]: item for item in old_document.get("datasets", [])}
    changes = []
    for item in descriptors:
        old = old_by_id.get(item["id"])
        if old != item:
            changes.append(
                {
                    "id": item["id"],
                    "fromVersion": old.get("release", {}).get("version") if old else None,
                    "toVersion": item["release"]["version"],
                    "upstreamUpdatedAt": item["externalArchive"]["upstreamUpdatedAt"],
                }
            )
    return descriptors, changes


def write_if_changed(path: Path, document: dict) -> bool:
    rendered = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text() == rendered:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, default=SELECTION_PATH)
    parser.add_argument("--no-build-catalog", action="store_true")
    args = parser.parse_args()

    descriptors, changes = sync(args.selection)
    changed = write_if_changed(POINTERS_PATH, {"datasets": descriptors})
    report = {
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "changed": changes,
        "pointerManifestChanged": changed,
    }
    write_if_changed(CACHE_REPORT_PATH, report)

    if changed and not args.no_build_catalog:
        subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "build_catalog.py")], check=True)
    print(f"Kiwix pointer sync: {len(changes)} changed; catalog {'updated' if changed else 'unchanged'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
