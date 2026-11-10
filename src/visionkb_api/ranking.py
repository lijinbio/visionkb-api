"""Gene-set ranking by specificity (kind=expressed, sort=specificity)."""

from __future__ import annotations

# CP10k. Same +1 offset as the log1p(CP10k) scale of the summaries (mean_log1p_cp10k); keeps the ratio finite and
# damps ratios of low-abundance genes. Sensitivity on visionkb-2026.11.0 (HRCA snRNA Rod, 5477 D18 candidates) is in
# the CHANGELOG (0.1.3): the leading rod markers and MALAT1's demotion hold at 0.01, 0.1 and 1.
PSEUDOCOUNT = 1.0
SCORE_DEFINITION = (
    "score = specificity of the gene's best supporting StudyResult = (pseudobulk_cp10k of that group + 1) / "
    "(max pseudobulk_cp10k of the gene over the OTHER groups of the same dataset and level + 1); ties by "
    "pct_expressing; null when the dataset has no other group at that level; sort=pct for the previous ranking "
    "(max pct_expressing)"
)


def _r(x: float | None) -> float | None:
    return None if x is None else round(float(x), 6)


def specificity(row: dict, top2: dict) -> tuple[float | None, float | None, str | None]:
    """(score, other-group max pseudobulk, that group) for one StudyResult row."""
    hit = top2.get((row.get("dataset_id"), row.get("level"), row.get("gene_id")))
    pb = row.get("pseudobulk_cp10k")
    if hit is None or pb is None:
        return None, None, None
    pbs, groups = hit
    if groups and groups[0] == row.get("group_label"):
        other, other_group = (pbs[1], groups[1]) if len(pbs) > 1 else (None, None)
    else:
        other, other_group = pbs[0], groups[0]
    if other is None:
        return None, None, None
    return (pb + PSEUDOCOUNT) / (other + PSEUDOCOUNT), other, other_group


def rank_by_specificity(rows: list[dict], top2: dict, limit: int, offset: int) -> tuple[list[dict], int]:
    """Aggregate StudyResult rows per gene (best specificity), sort, page."""
    genes: dict[str, dict] = {}
    for row in rows:
        score, other, other_group = specificity(row, top2)
        g = genes.setdefault(
            row["gene_id"], {"gene_id": row["gene_id"], "symbol": row.get("symbol"), "name": row.get("name"), "srs": []}
        )
        g["srs"].append((score, row.get("pct_expressing") or 0.0, row, other, other_group))
    items = []
    for g in genes.values():
        srs = sorted(g["srs"], key=lambda s: (s[0] is not None, s[0] or 0.0, s[1]), reverse=True)
        best_score, best_pct, best, other, other_group = srs[0]
        edges: dict[str, dict] = {}
        for score, _pct, row, _o, _og in srs:
            if row["edge_id"] not in edges:
                ev = {
                    k: row.get(k)
                    for k in (
                        "edge_id",
                        "predicate",
                        "target",
                        "target_name",
                        "knowledge_level",
                        "primary_knowledge_source",
                        "max_pct_expressing",
                        "max_mean_log1p_cp10k",
                        "n_datasets",
                        "evidence_count",
                    )
                }
                ev["specificity"] = _r(score)
                edges[row["edge_id"]] = {k: v for k, v in ev.items() if v is not None}
        items.append(
            {
                "gene_id": g["gene_id"],
                "symbol": g["symbol"],
                "name": g["name"],
                "score": _r(best_score),
                "score_kind": "specificity",
                "pct_expressing": _r(best_pct),
                "n_edges": len(edges),
                "best_study_result": {
                    "dataset_id": best.get("dataset_id"),
                    "level": best.get("level"),
                    "group_label": best.get("group_label"),
                    "target": best.get("target"),
                    "pseudobulk_cp10k": _r(best.get("pseudobulk_cp10k")),
                    "other_max_pseudobulk_cp10k": _r(other),
                    "other_max_group": other_group,
                },
                "evidence": list(edges.values())[:5],
            }
        )
    items.sort(
        key=lambda i: (i["score"] is None, -(i["score"] or 0.0), -(i["pct_expressing"] or 0.0), i["symbol"] or "")
    )
    return items[offset : offset + limit], len(items)
