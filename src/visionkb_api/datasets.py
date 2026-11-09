"""Dataset registry search (contracts §4): filters plus entity matching with bind records."""

from __future__ import annotations

from collections.abc import Callable

GENE_PREFIXES = ("HGNC:", "ENSEMBL:", "NCBIGene:", "visionkb:gene-")


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _bind(rec: dict) -> dict:
    bind = dict(rec.get("bind") or {})
    if not bind.get("path") and rec.get("h5ad_path"):
        bind["path"] = rec["h5ad_path"]
    if not bind.get("name"):
        bind["name"] = str(rec.get("id", "")).removeprefix("visionkb:ds-")
    bind.setdefault("role", "reference")
    return bind


def find_datasets(
    records: list[dict],
    *,
    entities: list[str],
    species: str | None = None,
    tissue: str | None = None,
    assay: str | None = None,
    disease: str | None = None,
    expressing: Callable[[str], dict[str, float]] | None = None,
    cell_states_under: Callable[[str], list[str]] | None = None,
) -> tuple[list[dict], list[str]]:
    """Return ranked registry records (with ``score``, ``matched``, ``bind``) and warnings."""
    warnings: list[str] = []
    expr_hits: dict[str, dict[str, float]] = {}
    states: dict[str, set[str]] = {}
    for e in entities:
        if e.startswith(GENE_PREFIXES):
            if expressing is None:
                warnings.append(f"{e}: expression summaries unavailable; gene not matched")
            else:
                expr_hits[e] = expressing(e)
        elif cell_states_under is not None and e.startswith("CL:"):
            try:
                states[e] = set(cell_states_under(e))
            except Exception:  # graph optional here: report, do not fail the registry search
                warnings.append(f"{e}: graph unavailable; CellState subclasses not expanded")
    out = []
    for rec in records:
        if species and species not in _as_list(rec.get("species")):
            continue
        if tissue and tissue not in _as_list(rec.get("tissue")):
            continue
        if assay and assay not in _as_list(rec.get("assay")):
            continue
        if disease and disease not in _as_list(rec.get("disease")):
            continue
        matched = []
        cts = set(_as_list(rec.get("cell_types")))
        for e in entities:
            fields = []
            if e in cts:
                fields.append({"entity": e, "field": "cell_types"})
            via = sorted(states.get(e, set()) & cts)
            if via:
                fields.append({"entity": e, "field": "cell_types", "via": via[:10]})
            for f in ("tissue", "species", "assay", "disease"):
                if e in _as_list(rec.get(f)):
                    fields.append({"entity": e, "field": f})
            if e in expr_hits and rec.get("id") in expr_hits[e]:
                fields.append(
                    {"entity": e, "field": "expression", "max_pct_expressing": round(expr_hits[e][rec["id"]], 4)}
                )
            if fields:
                matched.append(fields[0] if len(fields) == 1 else {"entity": e, "fields": fields})
        if entities and not matched:
            continue
        score = len(matched) / len(entities) if entities else 1.0
        row = dict(rec)
        row["bind"] = _bind(rec)
        row["score"] = round(score, 4)
        row["matched"] = matched
        out.append(row)
    out.sort(key=lambda r: (-r["score"], str(r.get("id"))))
    return out, warnings
