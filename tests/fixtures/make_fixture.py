"""Generate a tiny, synthetic but contract-shaped KG release bundle for tests.

Identifiers are real and were checked against their sources on 2026-11-09 (HGNC REST, EBI OLS4,
PubMed); every **expression value, count and dataset is synthetic**. Output, under ``--out``::

    <release>/RELEASE, manifest.json, kgx/{nodes,edges}.jsonl, neo4j-import/ (CSVs, import.args,
    post_import.cypher), summaries/expr_summary.parquet, registry/datasets.jsonl, validation/
    <release>.tar, <release>.tar.sha256          # the relocatable bundle (contracts v0.3 §1)

Usage: ``python tests/fixtures/make_fixture.py --out /path/to/fixtures`` (needs duckdb only).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

RELEASE = "visionkb-0000.01.0"
CREATED = "2026-11-09T00:00:00Z"
HUMAN = "NCBITaxon:9606"
RETINA, RPE_LAYER = "UBERON:0000966", "UBERON:0001782"

# HGNC (rest.genenames.org, 2026-11-09): symbol, id, name, alias + previous symbols, Ensembl gene.
GENES = [
    ("RHO", "HGNC:10012", "rhodopsin", ["OPN2", "CSNBAD1", "RP4"], "ENSG00000163914"),
    ("APLN", "HGNC:16665", "apelin", ["XNPEP2"], "ENSG00000171388"),
    ("ABCA4", "HGNC:34", "ATP binding cassette subfamily A member 4", ["ABCR", "STGD1", "RP19"], "ENSG00000198691"),
    ("RPE65", "HGNC:10294", "retinoid isomerohydrolase RPE65", ["LCA2", "RP20"], "ENSG00000116745"),
    ("GNAT1", "HGNC:4393", "G protein subunit alpha transducin 1", ["CSNBAD3"], "ENSG00000114349"),
    ("ARR3", "HGNC:710", "arrestin 3", ["ARRX"], "ENSG00000120500"),
    ("RLBP1", "HGNC:10024", "retinaldehyde binding protein 1", ["CRALBP"], "ENSG00000140522"),
    ("PECAM1", "HGNC:8823", "platelet and endothelial cell adhesion molecule 1", ["CD31"], "ENSG00000261371"),
]
# CL / UBERON / MONDO / HP / NCBITaxon labels and synonyms from OLS4 (2026-11-09).
CELLS = [
    ("CL:0000604", "retinal rod cell", ["rod"]),
    ("CL:0000573", "retinal cone cell", ["cone"]),
    ("CL:0002586", "retinal pigment epithelial cell", ["RPE"]),
    ("CL:0000636", "Mueller cell", ["Muller glia", "Müller cell", "Muller cell"]),
    ("CL:0000115", "endothelial cell", ["endotheliocyte"]),
    ("CL:0010009", "camera-type eye photoreceptor cell", []),
]
CELL_STATES = [
    ("visionkb:cs-hrca-snrna-ml-cone", "ML_Cone (hrca-snrna)", "CL:0000573"),
    ("visionkb:cs-hrca-snrna-s-cone", "S_Cone (hrca-snrna)", "CL:0000573"),
]
ANATOMY = [
    (RETINA, "retina", ["retina of camera-type eye"]),
    (RPE_LAYER, "pigmented layer of retina", ["RPE", "pigment epithelium of retina"]),
]
DISEASES = [
    ("MONDO:0013395", "retinitis pigmentosa 4", []),
    ("MONDO:0019200", "retinitis pigmentosa", ["Rod-cone dystrophy"]),
    ("MONDO:0019353", "Stargardt disease", []),
]
PHENOTYPES = [("HP:0000510", "Rod-cone dystrophy", ["Retinitis pigmentosa", "Rod cone dystrophy"])]
# Ontology parents verified in OLS4 (CL is_a, MONDO is_a).
SUBCLASS = [
    ("CL:0000604", "CL:0010009", "infores:cl"),
    ("CL:0000573", "CL:0010009", "infores:cl"),
    ("MONDO:0013395", "MONDO:0019200", "infores:mondo"),
]
# Gene-condition assertions with their primary references (PubMed esummary checked).
GENE_DISEASE = [
    (
        "HGNC:10012",
        "MONDO:0013395",
        "PMID:2137202",
        "Synthetic fixture text: a rhodopsin point mutation was reported in a family with dominant retinitis pigmentosa.",
    ),
    (
        "HGNC:34",
        "MONDO:0019353",
        "PMID:9054934",
        "Synthetic fixture text: the photoreceptor ABC transporter gene ABCR was reported mutated in Stargardt disease.",
    ),
]
DATASETS = [
    {
        "id": "visionkb:ds-hrca-snrna",
        "version": "heca-2026.05",
        "title": "SYNTHETIC FIXTURE: retina snRNA-seq (not real data)",
        "assay": "EFO:0009809",
        "tissue": [RETINA],
        "n_donors": 4,
        "groups": [
            ("majorclass", "Rod", "CL:0000604", None, 4000),
            ("majorclass", "Cone", "CL:0000573", None, 600),
            ("majorclass", "MG", "CL:0000636", None, 900),
            ("majorclass", "Endo", "CL:0000115", None, 15),
            ("celltype", "ML_Cone", "visionkb:cs-hrca-snrna-ml-cone", None, 520),
            ("celltype", "S_Cone", "visionkb:cs-hrca-snrna-s-cone", None, 80),
            ("region_majorclass", "fovea|Cone", "CL:0000573", "fovea", 250),
            ("region_majorclass", "periphery|Cone", "CL:0000573", "periphery", 350),
        ],
    },
    {
        "id": "visionkb:ds-rpe-scrna",
        "version": "heca-2026.05",
        "title": "SYNTHETIC FIXTURE: RPE/choroid scRNA-seq (not real data)",
        "assay": "EFO:0008913",
        "tissue": [RPE_LAYER],
        "n_donors": 3,
        "groups": [
            ("majorclass", "RPE", "CL:0002586", None, 1200),
            ("majorclass", "Endo", "CL:0000115", None, 300),
        ],
    },
]
# Synthetic fraction of expressing cells by group label (majorclass part of the label).
PROFILE = {
    "RHO": {"Rod": 0.95, "Cone": 0.15, "MG": 0.04},
    "GNAT1": {"Rod": 0.90, "Cone": 0.06},
    "ARR3": {"Cone": 0.92, "Rod": 0.03},
    "ABCA4": {"Rod": 0.72, "Cone": 0.61},
    "RPE65": {"RPE": 0.91},
    "RLBP1": {"MG": 0.86, "RPE": 0.74},
    "APLN": {"Endo": 0.34, "Rod": 0.01},
    "PECAM1": {"Endo": 0.93},
}
CELLTYPE_ADJ = {("ARR3", "S_Cone"): 0.80, ("ABCA4", "S_Cone"): 0.40, ("RHO", "fovea|Cone"): 0.12}
# Expression edge rule D18 (recorded in manifest.json expression_edge_rule): (a) pct >= 0.10 and n_cells >= 20,
# (b) pseudobulk_cp10k >= 0.10 x the gene's max over groups of the same dataset and level, (c) n_donors_expressing >= 2;
# majorclass and celltype levels only.
EDGE_RULE = {
    "decision": "D18",
    "levels": ["majorclass", "celltype"],
    "min_pct_expressing": 0.1,
    "min_n_cells": 20,
    "specificity_fraction": 0.1,
    "min_n_donors_expressing": 2,
}
# Synthetic donor support: one group expresses the gene in a single donor only (fails rule c).
DONOR_ADJ = {("ABCA4", "S_Cone"): 1}
# Synthetic known issue KI-1 (shape of the real validation/known_issues.json): one flagged group.
KI1_PAIRS = [
    {"dataset_id": "visionkb:ds-hrca-snrna", "group_label": "MG", "group_majorclass": "MG", "program_of": "Rod"}
]


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()  # deterministic id, not security


def edge_id(s: str, p: str, o: str, quals: dict) -> str:
    q = "|".join(f"{k}={quals[k]}" for k in sorted(quals))
    return "visionkb:e-" + sha1(f"{s}|{p}|{o}|{q}")


def pct_for(sym: str, label: str) -> float:
    if (sym, label) in CELLTYPE_ADJ:
        return CELLTYPE_ADJ[(sym, label)]
    major = label.split("|")[-1]
    if major in ("ML_Cone", "S_Cone"):
        major = "Cone"
    return PROFILE.get(sym, {}).get(major, 0.0)


def summary_rows() -> list[dict]:
    rows = []
    for ds in DATASETS:
        for level, label, ct, region, n_cells in ds["groups"]:
            for sym, gid, *_ in GENES:
                pct = pct_for(sym, label)
                n_exp = round(pct * n_cells)
                rows.append(
                    {
                        "dataset_id": ds["id"],
                        "atlas_version": ds["version"],
                        "level": level,
                        "group_label": label,
                        "cell_type_id": ct,
                        "region": region,
                        "gene_id": gid,
                        "gene_symbol": sym,
                        "var_symbol": sym,
                        "n_cells": n_cells,
                        "n_donors": ds["n_donors"],
                        "n_expressing": n_exp,
                        "pct_expressing": round(n_exp / n_cells, 6),
                        "mean_log1p_cp10k": round(2.6 * pct, 6),
                        "pseudobulk_cp10k": round(180.0 * pct * pct, 6),
                        "n_donors_expressing": DONOR_ADJ.get(
                            (sym, label),
                            ds["n_donors"] if pct >= 0.3 else (2 if pct >= 0.1 else (1 if pct >= 0.05 else 0)),
                        ),
                    }
                )
    return rows


def edge_reasons(rows: list[dict]) -> list[str | None]:
    """Per summary row: None if it becomes an expressed_in StudyResult under EDGE_RULE, else the first failing rule."""
    r = EDGE_RULE
    gmax: dict[tuple, float] = {}
    for x in rows:
        k = (x["dataset_id"], x["level"], x["gene_id"])
        gmax[k] = max(gmax.get(k, 0.0), x["pseudobulk_cp10k"])
    out: list[str | None] = []
    for x in rows:
        if x["level"] not in r["levels"]:
            out.append("level_not_in_graph")
        elif x["pct_expressing"] < r["min_pct_expressing"] or x["n_cells"] < r["min_n_cells"]:
            out.append("a_pct_or_n_cells")
        elif x["pseudobulk_cp10k"] < r["specificity_fraction"] * gmax[(x["dataset_id"], x["level"], x["gene_id"])]:
            out.append("b_specificity")
        elif x["n_donors_expressing"] < r["min_n_donors_expressing"]:
            out.append("c_donors")
        else:
            out.append(None)
    return out


def build_graph(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    for sym, gid, name, syn, ens in GENES:
        nodes.append(
            {
                "id": gid,
                "name": name,
                "category": ["biolink:Gene"],
                "symbol": sym,
                "synonym": syn,
                "xref": [f"ENSEMBL:{ens}"],
                "in_taxon": HUMAN,
                "provided_by": ["infores:hgnc"],
                "_labels": ["Gene", "NamedThing"],
            }
        )
    for cid, name, syn in CELLS:
        nodes.append(
            {
                "id": cid,
                "name": name,
                "category": ["biolink:Cell"],
                "synonym": syn,
                "provided_by": ["infores:cl"],
                "_labels": ["Cell", "AnatomicalEntity", "NamedThing"],
            }
        )
    for cid, name, _parent in CELL_STATES:
        nodes.append(
            {
                "id": cid,
                "name": name,
                "category": ["visionkb:CellState", "biolink:Cell"],
                "synonym": [name.split(" ")[0]],
                "in_taxon": HUMAN,
                "dataset_id": "visionkb:ds-hrca-snrna",
                "provided_by": ["infores:visionkb-heca"],
                "_labels": ["CellState", "Cell", "AnatomicalEntity", "NamedThing"],
            }
        )
    for table, cat, label, src in (
        (ANATOMY, "biolink:AnatomicalEntity", ["AnatomicalEntity", "NamedThing"], "infores:uberon"),
        (DISEASES, "biolink:Disease", ["Disease", "NamedThing"], "infores:mondo"),
        (PHENOTYPES, "biolink:PhenotypicFeature", ["PhenotypicFeature", "NamedThing"], "infores:hpo"),
    ):
        for nid, name, syn in table:
            nodes.append(
                {"id": nid, "name": name, "category": [cat], "synonym": syn, "provided_by": [src], "_labels": label}
            )
    nodes.append(
        {
            "id": HUMAN,
            "name": "Homo sapiens",
            "category": ["biolink:OrganismTaxon"],
            "synonym": ["human"],
            "provided_by": ["infores:ncbitaxon"],
            "_labels": ["OrganismTaxon", "NamedThing"],
        }
    )
    for ds in DATASETS:
        nodes.append(
            {
                "id": ds["id"],
                "name": ds["title"],
                "category": ["biolink:Dataset"],
                "version": ds["version"],
                "access_level": "lab",
                "provided_by": ["infores:visionkb-heca"],
                "_labels": ["Dataset", "NamedThing"],
            }
        )

    def onto_edge(s, o, src, pred="subclass_of"):
        p = f"biolink:{pred}"
        return {
            "id": edge_id(s, p, o, {}),
            "subject": s,
            "predicate": p,
            "object": o,
            "knowledge_level": "knowledge_assertion",
            "agent_type": "manual_agent",
            "primary_knowledge_source": src,
            "status": "curated",
            "visibility": "public",
            "kg_release": RELEASE,
            "_type": pred,
        }

    state_parents = [(cid, parent, "infores:visionkb-heca") for cid, _, parent in CELL_STATES]
    for s, o, src in SUBCLASS + state_parents:
        edges.append(onto_edge(s, o, src))

    # Expression: one edge per (gene, cell type, tissue, species); one StudyResult per dataset group.
    tissue = {ds["id"]: ds["tissue"][0] for ds in DATASETS}
    by_edge: dict[str, dict] = {}
    for r, why in zip(rows, edge_reasons(rows), strict=True):
        if why is not None:
            continue
        quals = {"anatomical_context_qualifier": tissue[r["dataset_id"]], "species_context_qualifier": HUMAN}
        eid = edge_id(r["gene_id"], "biolink:expressed_in", r["cell_type_id"], quals)
        e = by_edge.setdefault(
            eid,
            {
                "id": eid,
                "subject": r["gene_id"],
                "predicate": "biolink:expressed_in",
                "object": r["cell_type_id"],
                **quals,
                "knowledge_level": "observation",
                "agent_type": "data_analysis_pipeline",
                "primary_knowledge_source": "infores:visionkb-heca",
                "publications": [],
                "has_study_results": [],
                "status": "curated",
                "visibility": "lab",
                "kg_release": RELEASE,
                "_type": "expressed_in",
                "_datasets": set(),
                "max_pct_expressing": 0.0,
                "max_mean_log1p_cp10k": 0.0,
            },
        )
        srid = "visionkb:sr-" + sha1(f"{eid}|{r['dataset_id']}|{r['atlas_version']}|{r['level']}|{r['group_label']}")
        e["has_study_results"].append(srid)
        e["_datasets"].add(r["dataset_id"])
        e["max_pct_expressing"] = max(e["max_pct_expressing"], r["pct_expressing"])
        e["max_mean_log1p_cp10k"] = max(e["max_mean_log1p_cp10k"], r["mean_log1p_cp10k"])
        nodes.append(
            {
                "id": srid,
                "category": ["biolink:StudyResult"],
                "edge_id": eid,
                "dataset_id": r["dataset_id"],
                "atlas_version": r["atlas_version"],
                "level": r["level"],
                "group_label": r["group_label"],
                "n_cells": r["n_cells"],
                "n_donors": r["n_donors"],
                "pct_expressing": r["pct_expressing"],
                "mean_log1p_cp10k": r["mean_log1p_cp10k"],
                "pseudobulk_cp10k": r["pseudobulk_cp10k"],
                "n_donors_expressing": r["n_donors_expressing"],
                "method": "visionkb-atlas streamed summary (synthetic fixture values)",
                "provided_by": ["infores:visionkb-heca"],
                "_labels": ["StudyResult", "NamedThing"],
            }
        )
        edges.append({"id": None, "subject": srid, "object": r["dataset_id"], "_type": "supporting_data_set"})
    for e in by_edge.values():
        e["n_datasets"] = len(e.pop("_datasets"))
        e["evidence_count"] = len(e["has_study_results"])
        edges.append(e)

    for gid, mondo, pmid, text in GENE_DISEASE:
        p = "biolink:gene_associated_with_condition"
        eid = edge_id(gid, p, mondo, {})
        srid = "visionkb:sr-" + sha1(f"{eid}|{pmid}")
        nodes.append(
            {
                "id": srid,
                "category": ["biolink:StudyResult"],
                "edge_id": eid,
                "pmid": pmid,
                "supporting_text": text[:300],
                "grade": "fixture",
                "review_note": "synthetic fixture record",
                "provided_by": ["infores:visionkb-retigene"],
                "_labels": ["StudyResult", "NamedThing"],
            }
        )
        edges.append(
            {
                "id": eid,
                "subject": gid,
                "predicate": p,
                "object": mondo,
                "knowledge_level": "knowledge_assertion",
                "agent_type": "manual_validation_of_automated_agent",
                "primary_knowledge_source": "infores:visionkb-retigene",
                "publications": [pmid],
                "has_study_results": [srid],
                "evidence_count": 1,
                "status": "curated",
                "visibility": "lab",
                "kg_release": RELEASE,
                "_type": "gene_associated_with_condition",
            }
        )
    return nodes, edges


# -- neo4j-admin CSVs --------------------------------------------------------------------------
INT_PROPS = {"n_cells", "n_donors", "n_donors_expressing", "evidence_count", "n_datasets"}
FLOAT_PROPS = {"pct_expressing", "mean_log1p_cp10k", "pseudobulk_cp10k", "max_pct_expressing", "max_mean_log1p_cp10k"}


def _header(key: str, values: list) -> str:
    if any(isinstance(v, list) for v in values):
        return f"{key}:string[]"
    if key in INT_PROPS:
        return f"{key}:int"
    if key in FLOAT_PROPS:
        return f"{key}:float"
    return key


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        for x in v:
            if ";" in str(x):
                raise ValueError(f"array value contains the delimiter: {x!r}")
        return ";".join(str(x) for x in v)
    return str(v)


def write_csv(path: Path, fixed: list[str], rows: list[dict], skip: set[str]) -> None:
    keys = sorted({k for r in rows for k in r if k not in skip and not k.startswith("_")})
    headers = fixed + [_header(k, [r.get(k) for r in rows]) for k in keys]
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(headers)
    for r in rows:
        wr.writerow([r["__fixed"][i] for i in range(len(fixed))] + [_cell(r.get(k)) for k in keys])
    path.write_text(buf.getvalue(), encoding="utf-8")


POST_IMPORT = """// Run once after neo4j-admin import, e.g. cypher-shell -f post_import.cypher
CREATE CONSTRAINT namedthing_id IF NOT EXISTS FOR (n:NamedThing) REQUIRE n.id IS UNIQUE;
CREATE FULLTEXT INDEX entity_search IF NOT EXISTS FOR (n:NamedThing) ON EACH [n.name, n.symbol, n.synonym];
CREATE INDEX studyresult_edge_id IF NOT EXISTS FOR (n:StudyResult) ON (n.edge_id);
CREATE INDEX gene_symbol IF NOT EXISTS FOR (n:Gene) ON (n.symbol);
{rel_indexes}
CALL db.awaitIndexes(300);
"""


def write_neo4j(out: Path, nodes: list[dict], edges: list[dict]) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    args = []
    by_label: dict[str, list[dict]] = {}
    for n in nodes:
        by_label.setdefault(n["_labels"][0], []).append(dict(n, __fixed=[n["id"], ";".join(n["_labels"])]))
    for label in sorted(by_label):
        fn = f"nodes_{label}.csv"
        # neo4j-admin stores the `id:ID` column as the node property `id`.
        write_csv(out / fn, ["id:ID", ":LABEL"], by_label[label], {"id"})
        args.append(f"--nodes=./{fn}")
    by_type: dict[str, list[dict]] = {}
    for e in edges:
        by_type.setdefault(e["_type"], []).append(dict(e, __fixed=[e["subject"], e["object"], e["_type"]]))
    for t in sorted(by_type):
        fn = f"rels_{t}.csv"
        write_csv(
            out / fn,
            [":START_ID", ":END_ID", ":TYPE"],
            by_type[t],
            {"subject", "object"} | ({"id"} if t == "supporting_data_set" else set()),
        )
        args.append(f"--relationships=./{fn}")
    (out / "import.args").write_text("\n".join(args) + "\n", encoding="utf-8")
    rel_idx = "\n".join(
        f"CREATE INDEX rel_id_{t} IF NOT EXISTS FOR ()-[r:{t}]-() ON (r.id);"
        for t in sorted(by_type)
        if t != "supporting_data_set"
    )
    (out / "post_import.cypher").write_text(POST_IMPORT.format(rel_indexes=rel_idx), encoding="utf-8")
    return args


def write_parquet(path: Path, rows: list[dict]) -> None:
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        ("dataset_id", "VARCHAR"),
        ("atlas_version", "VARCHAR"),
        ("level", "VARCHAR"),
        ("group_label", "VARCHAR"),
        ("cell_type_id", "VARCHAR"),
        ("region", "VARCHAR"),
        ("gene_id", "VARCHAR"),
        ("gene_symbol", "VARCHAR"),
        ("var_symbol", "VARCHAR"),
        ("n_cells", "BIGINT"),
        ("n_donors", "BIGINT"),
        ("n_expressing", "BIGINT"),
        ("pct_expressing", "DOUBLE"),
        ("mean_log1p_cp10k", "DOUBLE"),
        ("pseudobulk_cp10k", "DOUBLE"),
        ("n_donors_expressing", "BIGINT"),
    ]
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (" + ", ".join(f"{c} {t}" for c, t in cols) + ")")
    con.executemany(
        "INSERT INTO t VALUES (" + ", ".join("?" for _ in cols) + ")", [[r[c] for c, _ in cols] for r in rows]
    )
    literal = str(path).replace("'", "''")
    con.execute(
        f"COPY (SELECT * FROM t ORDER BY dataset_id, level, group_label, gene_id) TO '{literal}' (FORMAT parquet)"
    )
    con.close()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def registry(rows: list[dict]) -> list[dict]:
    out = []
    for ds in DATASETS:
        slug = ds["id"].removeprefix("visionkb:ds-")
        cts = sorted({g[2] for g in ds["groups"]})
        out.append(
            {
                "id": ds["id"],
                "version": ds["version"],
                "title": ds["title"],
                "species": HUMAN,
                "tissue": ds["tissue"],
                "assay": ds["assay"],
                "disease": None,
                "stage": "adult",
                "n_cells": sum(g[4] for g in ds["groups"] if g[0] == "majorclass"),
                "n_donors": ds["n_donors"],
                "n_samples": ds["n_donors"],
                "levels": sorted({g[0] for g in ds["groups"]}),
                "cell_types": cts,
                "format": "soma",
                "h5ad_path": f"/path/to/atlas/{slug}.h5ad",
                "soma_uri": f"/path/to/visionkb/atlas/{ds['id']}/{ds['version']}/soma",
                "checksum": None,
                "access_level": "lab",
                "license": "fixture",
                "citation": None,
                "kg_release": RELEASE,
                "bind": {"path": f"/path/to/atlas/{slug}.h5ad", "name": slug, "role": "reference"},
            }
        )
    return out


def strip(d: dict) -> dict:
    return {
        k: (sorted(v) if isinstance(v, set) else v) for k, v in d.items() if not k.startswith("_") and v is not None
    }


def make(out_root: Path, release: str = RELEASE) -> Path:
    global RELEASE
    RELEASE = release
    rel = out_root / release
    if rel.exists():
        shutil.rmtree(rel)
    rel.mkdir(parents=True)
    rows = summary_rows()
    nodes, edges = build_graph(rows)
    (rel / "RELEASE").write_text(release + "\n", encoding="utf-8")
    (rel / "kgx").mkdir()
    (rel / "kgx/nodes.jsonl").write_text("".join(json.dumps(strip(n), sort_keys=True) + "\n" for n in nodes))
    (rel / "kgx/edges.jsonl").write_text(
        "".join(
            json.dumps(strip(dict(e, predicate=e.get("predicate", "biolink:supporting_data_set"))), sort_keys=True)
            + "\n"
            for e in edges
        )
    )
    write_neo4j(rel / "neo4j-import", nodes, edges)
    write_parquet(rel / "summaries/expr_summary.parquet", rows)
    (rel / "registry").mkdir()
    (rel / "registry/datasets.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in registry(rows)), encoding="utf-8"
    )
    (rel / "validation").mkdir()
    reasons = edge_reasons(rows)
    exclusions = {k: reasons.count(k) for k in ("level_not_in_graph", "a_pct_or_n_cells", "b_specificity", "c_donors")}
    exclusions["kept_study_results"] = reasons.count(None)
    report = {
        "kg_release": release,
        "status": "pass",
        "n_errors": 0,
        "n_warnings": 0,
        "note": "synthetic fixture",
        "expression_edge_rule_exclusions": exclusions,
    }
    ki = {
        "id": "KI-1",
        "title": "cross-lineage marker programs in cell-type groups (SYNTHETIC FIXTURE)",
        "method": "synthetic fixture record with the shape of the real known_issues.json",
        "n_groups": len(KI1_PAIRS),
        "n_pairs": len(KI1_PAIRS),
        "pairs": KI1_PAIRS,
    }
    (rel / "validation/known_issues.json").write_text(json.dumps(ki, indent=1) + "\n")
    (rel / "validation/report.json").write_text(json.dumps(report, indent=1) + "\n")
    (rel / "validation/report.md").write_text(f"# Validation {release}\n\nSynthetic fixture: no checks run.\n")
    counts_nodes: dict[str, int] = {}
    for n in nodes:
        for c in n["category"]:
            counts_nodes[c] = counts_nodes.get(c, 0) + 1
    counts_edges: dict[str, int] = {}
    for e in edges:
        p = e.get("predicate") or "supporting_data_set"
        counts_edges[p] = counts_edges.get(p, 0) + 1
    files = []
    for p in sorted(rel.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files.append({"path": p.relative_to(rel).as_posix(), "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    summary_sha = sha256_file(rel / "summaries/expr_summary.parquet")
    manifest = {
        "kg_release": release,
        "created_utc": CREATED,
        "biolink_version": "4.4.6",
        "schema_version": "fixture",
        "code": {"visionkb-api-fixture": "tests/fixtures/make_fixture.py"},
        "sources": [
            {"name": "HGNC", "version": "2026-11-09", "license": "CC0", "infores": "infores:hgnc"},
            {"name": "CL", "version": "OLS4 2026-11-09", "license": "CC-BY-4.0", "infores": "infores:cl"},
            {"name": "MONDO", "version": "OLS4 2026-11-09", "license": "CC-BY-4.0", "infores": "infores:mondo"},
            {
                "name": "synthetic expression",
                "version": "fixture",
                "license": "fixture",
                "infores": "infores:visionkb-heca",
            },
        ],
        "atlases": [
            {"dataset_id": d["id"], "atlas_version": d["version"], "summary_sha256": summary_sha} for d in DATASETS
        ],
        "counts": {
            "nodes_by_category": dict(sorted(counts_nodes.items())),
            "edges_by_predicate": dict(sorted(counts_edges.items())),
            "study_results": counts_nodes.get("biolink:StudyResult", 0),
        },
        "validation": {"status": "pass", "n_errors": 0, "n_warnings": 0},
        "expression_edge_rule": dict(EDGE_RULE, donor_support_checked={d["id"]: True for d in DATASETS}),
        "known_issues": [
            {
                "id": "KI-1",
                "title": ki["title"],
                "status": "open; report only (synthetic fixture)",
                "n_groups": ki["n_groups"],
                "n_pairs": ki["n_pairs"],
                "details": "validation/known_issues.json",
                "section": "validation/report.md#known-issues",
                "rod_program_groups": [f"{p['dataset_id']}|{p['group_label']}" for p in KI1_PAIRS],
            }
        ],
        "files": files,
    }
    (rel / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    # Relocatable bundle: tar of the release folder, relative member names, fixed metadata.
    tar_path = out_root / f"{release}.tar"

    def norm(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mtime = 0
        ti.mode = 0o755 if ti.isdir() else 0o644
        return ti

    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tf:
        for p in [rel, *sorted(rel.rglob("*"))]:
            tf.add(p, arcname=p.relative_to(out_root).as_posix(), recursive=False, filter=norm)
    (out_root / f"{release}.tar.sha256").write_text(f"{sha256_file(tar_path)}  {tar_path.name}\n")
    return rel


def main() -> None:
    ap = argparse.ArgumentParser(description="write the synthetic VisionKB fixture release")
    default = Path(os.environ.get("VISIONKB_WORK_DIR", ".")) / "visionkb-api" / "fixtures"
    ap.add_argument("--out", type=Path, default=default)
    ap.add_argument("--release", default=RELEASE)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rel = make(a.out, a.release)
    print(rel)


if __name__ == "__main__":
    main()
