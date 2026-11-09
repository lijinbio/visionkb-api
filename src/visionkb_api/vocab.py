"""Biolink vocabulary (vendored, see ``scripts/vendor_biolink.py``) and identifier checks."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources

BIOLINK_VERSION = "4.4.6"

# Prefixes the KG uses (Biolink id_prefixes order for genes, cell types, anatomy, disease,
# phenotype, assays, taxa) plus local and provenance prefixes. Unknown prefixes are rejected.
_LOCAL = r"[A-Za-z0-9][A-Za-z0-9._\-]{0,150}"
PREFIX_PATTERNS: dict[str, re.Pattern[str]] = {
    "HGNC": re.compile(r"\d{1,6}"),
    "NCBIGene": re.compile(r"\d{1,10}"),
    "ENSEMBL": re.compile(r"ENS[A-Z]*[GTP]\d{11}(\.\d+)?"),
    "UniProtKB": re.compile(r"[A-Z0-9]{6,10}(-\d+)?"),
    "CL": re.compile(r"\d{7}"),
    "UBERON": re.compile(r"\d{7}"),
    "MONDO": re.compile(r"\d{7}"),
    "HP": re.compile(r"\d{7}"),
    "EFO": re.compile(r"\d{7}"),
    "GO": re.compile(r"\d{7}"),
    "CHEBI": re.compile(r"\d{1,7}"),
    "PR": re.compile(r"[A-Z0-9]{6,12}"),
    "OMIM": re.compile(r"\d{6}"),
    "Orphanet": re.compile(r"\d{1,7}"),
    "NCBITaxon": re.compile(r"\d{1,8}"),
    "PMID": re.compile(r"\d{1,9}"),
    "doi": re.compile(r"10\.\d{4,9}/\S{1,200}"),
    "DOI": re.compile(r"10\.\d{4,9}/\S{1,200}"),
    "infores": re.compile(r"[a-z0-9][a-z0-9.\-]{0,100}"),
    "visionkb": re.compile(_LOCAL),
}
CURIE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):(\S+)$")
EDGE_ID_RE = re.compile(r"^visionkb:e-[0-9a-f]{40}$")
MAX_CURIE_LEN = 220


@lru_cache(maxsize=1)
def biolink() -> dict:
    ref = resources.files("visionkb_api") / "data" / f"biolink-{BIOLINK_VERSION}.json"
    return json.loads(ref.read_text(encoding="utf-8"))


def is_curie(text: str) -> bool:
    """True if ``text`` is a CURIE with a known prefix and a well-formed local id."""
    if not text or len(text) > MAX_CURIE_LEN:
        return False
    m = CURIE_RE.match(text)
    if not m:
        return False
    pat = PREFIX_PATTERNS.get(m.group(1))
    return bool(pat and pat.fullmatch(m.group(2)))


def looks_like_curie(text: str) -> bool:
    """True if ``text`` has CURIE shape (prefix:local), known prefix or not."""
    return bool(text) and len(text) <= MAX_CURIE_LEN and bool(CURIE_RE.match(text)) and " " not in text


def normalize_predicate(pred: str) -> str:
    """``expressed_in`` or ``biolink:expressed_in`` -> ``biolink:expressed_in``."""
    p = pred.strip()
    return p if p.startswith("biolink:") else f"biolink:{p}"


def predicate_info(pred: str) -> dict | None:
    return biolink()["predicates"].get(normalize_predicate(pred))


def is_category(cat: str) -> bool:
    c = cat if ":" in cat else f"biolink:{cat}"
    return c in biolink()["categories"] or c == "visionkb:CellState"


def normalize_category(cat: str) -> str:
    c = cat.strip()
    return c if ":" in c else f"biolink:{c}"


def qualifier_names() -> set[str]:
    return set(biolink()["qualifiers"])


def knowledge_levels() -> list[str]:
    return list(biolink()["knowledge_level"])


def agent_types() -> list[str]:
    return list(biolink()["agent_type"])
