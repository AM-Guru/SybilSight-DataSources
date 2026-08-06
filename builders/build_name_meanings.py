#!/usr/bin/env python3
"""name-meanings — first and family names with their real meaning and origin.

Source: the English Wiktionary pages-articles dump (CC BY-SA 4.0), ranked by
Wikidata bearer counts (CC0).

Why Wiktionary and not Wikidata: Wikidata name items exist, but their English
``schema:description`` is a type label — "female given name", "family name" —
not an etymology. A dataset built from those descriptions answers every single
query with "female given name", which is worse than having no dataset at all
because it looks like it worked. Wiktionary carries the actual content:

    ===Etymology===
    First used by {{w|William Shakespeare}} in ''{{w|Cymbeline}}'', a misprint
    for [[Innogen]], from {{der|en|gd|inghean||girl, maiden}}

    # {{lb|en|chiefly|British}} {{given name|en|female|from=Celtic languages}}.

So the work here is wikitext: locate the name senses, pull the etymology
section, and render the handful of templates that carry meaning into prose.
Unrenderable templates are dropped rather than emitted raw — a meaning line
reading "from {{der|en|gd|...}}" on the glasses is worse than a shorter one.
"""

from __future__ import annotations

import bz2
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import download, log, name_key, new_container, package, write_meta  # noqa: E402
from wikidata import sparql  # noqa: E402

DATASET_ID = "name-meanings"
TARGET_COUNT = 50_000

DUMP_URL = "https://dumps.wikimedia.org/enwiktionary/latest/enwiktionary-latest-pages-articles.xml.bz2"

# Two narrow queries rather than one with a label join. The joined form —
# GROUP BY over every human crossed with a label lookup — is heavy enough that
# the endpoint returns 502 rather than finishing, and losing the ranking would
# silently ship the 50,000 most obscure names instead of the most common.
BEARER_QUERY = """
SELECT ?name (COUNT(?person) AS ?bearers) WHERE {
  ?person wdt:P31 wd:Q5 .
  ?person wdt:%s ?name .
}
GROUP BY ?name
"""

NAME_LABEL_QUERY = """
SELECT ?item ?itemLabel WHERE {
  VALUES ?class { wd:Q11879590 wd:Q12308941 wd:Q3409032 wd:Q202444 wd:Q101352 }
  ?item wdt:P31 ?class .
  ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "en")
}
"""

GIVEN_TEMPLATE = re.compile(r"\{\{given name\s*\|([^}]*)\}\}", re.IGNORECASE)
SURNAME_TEMPLATE = re.compile(r"\{\{surname\s*\|([^}]*)\}\}", re.IGNORECASE)
ETYMOLOGY_SECTION = re.compile(
    r"^={3,4}\s*Etymology[^=]*\s*={3,4}\s*\n(.*?)(?=\n={2,4}[^=]|\Z)",
    re.MULTILINE | re.DOTALL,
)
ENGLISH_SECTION = re.compile(r"^==\s*English\s*==\s*\n(.*?)(?=\n==[^=]|\Z)", re.MULTILINE | re.DOTALL)

# Wiktionary language codes seen in name etymologies. Unmapped codes fall
# through as the raw code, which is still more informative than dropping the
# clause entirely.
LANGUAGES = {
    "ang": "Old English", "ar": "Arabic", "arc": "Aramaic", "az": "Azerbaijani",
    "bg": "Bulgarian", "ca": "Catalan", "cel": "Celtic", "cs": "Czech",
    "cy": "Welsh", "da": "Danish", "de": "German", "el": "Greek",
    "en": "English", "enm": "Middle English", "es": "Spanish", "eu": "Basque",
    "fa": "Persian", "fi": "Finnish", "fr": "French", "fro": "Old French",
    "ga": "Irish", "gd": "Scottish Gaelic", "gem-pro": "Proto-Germanic",
    "gmw": "West Germanic", "goh": "Old High German", "grc": "Ancient Greek",
    "he": "Hebrew", "hi": "Hindi", "hu": "Hungarian", "hy": "Armenian",
    "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "la": "Latin", "lt": "Lithuanian", "lv": "Latvian",
    "mga": "Middle Irish", "nl": "Dutch", "no": "Norwegian", "non": "Old Norse",
    "pl": "Polish", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian",
    "sa": "Sanskrit", "sga": "Old Irish", "sh": "Serbo-Croatian", "sk": "Slovak",
    "sl": "Slovene", "sq": "Albanian", "sv": "Swedish", "sw": "Swahili",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese",
    "yi": "Yiddish", "zh": "Chinese", "ine-pro": "Proto-Indo-European",
    "sla-pro": "Proto-Slavic", "itc-pro": "Proto-Italic", "cel-pro": "Proto-Celtic",
    # Etymology-only codes. Without these the rendered meaning reads
    # "from xno “Jehan”", which looks like a rendering bug to a reader and is
    # useless as a fact.
    "xno": "Anglo-Norman", "la-lat": "Late Latin", "la-med": "Medieval Latin",
    "la-vul": "Vulgar Latin", "la-ecc": "Ecclesiastical Latin",
    "grc-koi": "Koine Greek", "gkm": "Byzantine Greek", "osx": "Old Saxon",
    "odt": "Old Dutch", "dum": "Middle Dutch", "gmh": "Middle High German",
    "gml": "Middle Low German", "nds": "Low German", "frm": "Middle French",
    "pro": "Old Provençal", "oc": "Occitan", "gl": "Galician", "ast": "Asturian",
    "an": "Aragonese", "co": "Corsican", "sc": "Sardinian", "rm": "Romansch",
    "fy": "West Frisian", "gv": "Manx", "kw": "Cornish", "br": "Breton",
    "sga-pro": "Primitive Irish", "owl": "Old Welsh", "wlm": "Middle Welsh",
    "orv": "Old East Slavic", "cu": "Old Church Slavonic", "be": "Belarusian",
    "mk": "Macedonian", "sr": "Serbian", "hr": "Croatian", "bs": "Bosnian",
    "et": "Estonian", "smi": "Sami", "kl": "Greenlandic", "fo": "Faroese",
    "gmq-oda": "Old Danish", "gmq-osw": "Old Swedish", "nb": "Norwegian Bokmål",
    "nn": "Norwegian Nynorsk", "ka": "Georgian", "hyx-pro": "Proto-Armenian",
    "syc": "Classical Syriac", "akk": "Akkadian", "sux": "Sumerian",
    "egy": "Egyptian", "cop": "Coptic", "am": "Amharic", "ha": "Hausa",
    "yo": "Yoruba", "ig": "Igbo", "zu": "Zulu", "xh": "Xhosa", "st": "Sotho",
    "mi": "Māori", "haw": "Hawaiian", "sm": "Samoan", "to": "Tongan",
    "fj": "Fijian", "tl": "Tagalog", "ms": "Malay", "jv": "Javanese",
    "th": "Thai", "km": "Khmer", "lo": "Lao", "my": "Burmese", "bo": "Tibetan",
    "mn": "Mongolian", "kk": "Kazakh", "uz": "Uzbek", "ky": "Kyrgyz",
    "tt": "Tatar", "ota": "Ottoman Turkish", "ps": "Pashto", "ku": "Kurdish",
    "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati", "mr": "Marathi",
    "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
    "si": "Sinhala", "ne": "Nepali", "pi": "Pali", "inc-pro": "Proto-Indo-Aryan",
    "ira-pro": "Proto-Iranian", "peo": "Old Persian", "ae": "Avestan",
    "sem-pro": "Proto-Semitic", "urj-pro": "Proto-Uralic", "trk-pro": "Proto-Turkic",
    "bat-pro": "Proto-Balto-Slavic", "gem": "Germanic", "roa": "Romance",
}


def language(code: str) -> str:
    return LANGUAGES.get(code.strip(), code.strip())


def render_wikitext(text: str) -> str:
    """Flatten the templates that actually carry meaning; drop the rest.

    Order matters: derivation templates are rendered before the generic link
    templates, because the generic pass would otherwise eat their arguments.
    """
    # {{der|en|gd|inghean||girl, maiden}} → Scottish Gaelic “inghean” (“girl, maiden”)
    def derivation(match: re.Match) -> str:
        parts = [p.strip() for p in match.group(2).split("|")]
        if len(parts) < 2:
            return ""
        source = language(parts[1])
        term = parts[2] if len(parts) > 2 else ""
        gloss = ""
        # Positional form is |term|alt|gloss — an empty alt is the common shape.
        if len(parts) > 4 and parts[4]:
            gloss = parts[4]
        elif len(parts) > 3 and parts[3]:
            gloss = parts[3]
        for part in parts:
            if part.startswith("t="):
                gloss = part[2:]
        rendered = source
        if term and term != "-":
            rendered += f" “{term}”"
        if gloss:
            rendered += f" (“{gloss}”)"
        return rendered

    text = re.sub(
        r"\{\{(der|inh|bor|uder|derived|inherited|borrowed|cog|calque)\|([^}]*)\}\}",
        derivation,
        text,
        flags=re.IGNORECASE,
    )

    # {{m|he|יוֹחָנָן|t=God is gracious}} / {{l|en|John}}
    def link(match: re.Match) -> str:
        parts = [p.strip() for p in match.group(2).split("|")]
        if len(parts) < 2:
            return ""
        term = parts[1]
        gloss = next((p[2:] for p in parts if p.startswith("t=")), "")
        if len(parts) > 3 and parts[3] and not parts[3].startswith(("t=", "tr=", "pos=")):
            gloss = gloss or parts[3]
        return f"“{term}”" + (f" (“{gloss}”)" if gloss else "")

    text = re.sub(r"\{\{(m|l|mention|link)\|([^}]*)\}\}", link, text, flags=re.IGNORECASE)

    # {{w|William Shakespeare}} → William Shakespeare
    text = re.sub(r"\{\{w\|([^}|]*)(?:\|[^}]*)?\}\}", r"\1", text, flags=re.IGNORECASE)
    # {{lb|en|chiefly|British}} → (chiefly, British)
    text = re.sub(
        r"\{\{lb\|[a-z-]+\|([^}]*)\}\}",
        lambda m: "(" + ", ".join(p.strip() for p in m.group(1).split("|") if p.strip()) + ")",
        text,
        flags=re.IGNORECASE,
    )
    # Everything else in braces is machinery, not content.
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    # [[Innogen]] / [[Innogen|Imogen]]
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]|]*)\]\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)          # bold/italic markup
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:)])", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    return text.strip(" .,;:—-")


def sense_from_template(arguments: str) -> tuple[str, str]:
    """Return (gender, origin hint) from a {{given name}} / {{surname}} call."""
    parts = [p.strip() for p in arguments.split("|")]
    gender = ""
    origin = ""
    for part in parts:
        lowered = part.lower()
        if lowered in ("male", "female", "unisex"):
            gender = {"male": "Usually male", "female": "Usually female", "unisex": "Unisex"}[lowered]
        elif lowered.startswith("from="):
            origin = part[5:].strip()
        elif lowered.startswith("or="):
            gender = "Unisex"
    return gender, origin


def iter_pages(path: Path):
    with bz2.open(path, "rb") as handle:
        title = None
        for _, element in ET.iterparse(handle, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "title":
                title = (element.text or "").strip()
            elif tag == "page":
                text_node = element.find(".//{*}text")
                text = (text_node.text or "") if text_node is not None else ""
                if title and text:
                    yield title, text
                title = None
                element.clear()


def fetch_bearer_counts() -> dict[str, int]:
    """Name string → number of Wikidata humans bearing it.

    Ranking only. If the endpoint is unavailable the build continues unranked
    rather than aborting: an unranked dataset is degraded, but a failed build
    publishes nothing at all.
    """
    try:
        by_qid: dict[str, int] = {}
        for prop in ("P735", "P734"):
            log(f"counting bearers ({prop})")
            for row in sparql(BEARER_QUERY % prop):
                qid = row.get("name", "")
                if not qid:
                    continue
                try:
                    by_qid[qid] = by_qid.get(qid, 0) + int(row.get("bearers", "0"))
                except ValueError:
                    continue

        log("resolving name labels")
        counts: dict[str, int] = {}
        for row in sparql(NAME_LABEL_QUERY):
            qid = row.get("item", "")
            label = row.get("itemLabel", "").strip()
            if not qid or not label or qid not in by_qid:
                continue
            counts[label] = counts.get(label, 0) + by_qid[qid]
        return counts
    except SystemExit as error:
        log(f"WARNING: bearer counts unavailable ({error}); building unranked")
        return {}


def build() -> None:
    dump = download(DUMP_URL, "enwiktionary-latest-pages-articles.xml.bz2")
    counts = fetch_bearer_counts()
    log(f"{len(counts):,} names with a Wikidata bearer count")

    records: list[dict] = []
    scanned = 0
    for title, text in iter_pages(dump):
        scanned += 1
        if scanned % 500_000 == 0:
            log(f"  scanned {scanned:,} pages, kept {len(records):,} senses")
        # Names are capitalised single tokens; skipping the rest early keeps the
        # regex work off 8 million ordinary dictionary entries.
        if not title or not title[0].isupper() or ":" in title or " " in title:
            continue
        english = ENGLISH_SECTION.search(text)
        if not english:
            continue
        body = english.group(1)
        given_match = GIVEN_TEMPLATE.search(body)
        surname_match = SURNAME_TEMPLATE.search(body)
        if not given_match and not surname_match:
            continue

        etymology_match = ETYMOLOGY_SECTION.search(body)
        etymology = render_wikitext(etymology_match.group(1)) if etymology_match else ""
        if len(etymology) > 300:
            etymology = etymology[:297].rsplit(" ", 1)[0] + "…"

        rank = counts.get(title, 0)
        for part, match in (("given", given_match), ("family", surname_match)):
            if not match:
                continue
            gender, origin = sense_from_template(match.group(1))
            meaning = etymology
            if not meaning:
                # No etymology section: the template's own `from=` is still a
                # real answer ("a surname from Polish"), just a shorter one.
                if not origin:
                    continue
                meaning = f"A {'given name' if part == 'given' else 'surname'} from {origin}"
            records.append(
                {
                    "qid": f"{title}#{part}",
                    "name": title,
                    "name_key": name_key(title),
                    "part": part,
                    "meaning": meaning,
                    "origin": origin,
                    "gender": gender if part == "given" else "",
                    "notes": "",
                    "rank": rank,
                }
            )

    if not records:
        raise SystemExit("no name senses parsed — refusing to publish an empty dataset")

    records.sort(key=lambda r: (-r["rank"], r["name"]))
    records = records[:TARGET_COUNT]
    log(
        f"{len(records):,} senses retained "
        f"({len({r['name_key'] for r in records}):,} distinct keys) from {scanned:,} pages"
    )

    with new_container(DATASET_ID) as connection:
        connection.executescript(
            """
            CREATE TABLE name (
                qid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                part TEXT NOT NULL,
                meaning TEXT NOT NULL,
                origin TEXT,
                gender TEXT,
                notes TEXT,
                rank INTEGER NOT NULL
            );
            CREATE INDEX name_lookup ON name (name_key, part, rank DESC);
            """
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO name
                (qid, name, name_key, part, meaning, origin, gender, notes, rank)
            VALUES (:qid, :name, :name_key, :part, :meaning, :origin, :gender, :notes, :rank)
            """,
            records,
        )
        write_meta(
            connection,
            DATASET_ID,
            record_count=len(records),
            source="English Wiktionary name entries, ranked by Wikidata bearer counts",
            license="CC BY-SA 4.0",
        )

    package(DATASET_ID, compression="gzip")


if __name__ == "__main__":
    build()
