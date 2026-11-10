"""Unit tests on the fixture release files (DuckDB, registry, manifest). No databases needed."""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

import pytest
from conftest import TOKENS, auth
from fastapi.testclient import TestClient

from visionkb_api.app import create_app
from visionkb_api.auth import parse_tokens
from visionkb_api.config import ConfigError, Settings, read_secret
from visionkb_api.envelope import dumps, envelope, fit
from visionkb_api.graph import lucene_query
from visionkb_api.ranking import PSEUDOCOUNT, rank_by_specificity
from visionkb_api.release import load_release
from visionkb_api.summaries import Summaries
from visionkb_api.vocab import is_curie, predicate_info

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def client(release_dir, tokens_file):
    s = Settings(release_dir=release_dir, tokens_file=tokens_file)
    with TestClient(create_app(s)) as c:
        yield c


def ok(r, code=200):
    assert r.status_code == code, r.text
    body = r.json()
    assert body["status"] == "ok" and body["error"] is None
    assert body["kg_release"] == "visionkb-0000.01.0"
    return body


def err(r, code, ecode):
    assert r.status_code == code, r.text
    body = r.json()
    assert body["status"] == "error" and body["error"]["code"] == ecode, body
    assert body["kg_release"]
    assert body["data"] is None
    return body


# -- vocabulary and helpers --------------------------------------------------------------------
@pytest.mark.parametrize(
    "curie,good",
    [
        ("HGNC:10012", True),
        ("CL:0000604", True),
        ("MONDO:0013395", True),
        ("visionkb:cs-hrca-snrna-ml-cone", True),
        ("ENSEMBL:ENSG00000163914", True),
        ("CL:604", False),
        ("FOO:1", False),
        ("HGNC:10012' OR 1=1", False),
        ("rhodopsin", False),
        ("", False),
    ],
)
def test_is_curie(curie, good):
    assert is_curie(curie) is good


def test_biolink_vocab():
    assert predicate_info("expressed_in")["deprecated"] is False
    assert predicate_info("biolink:gene_associated_with_condition") is not None
    assert predicate_info("biolink:genetically_associated_with")["deprecated"] is True
    assert predicate_info("biolink:supporting_data_set") is None  # an association slot, not a predicate
    assert predicate_info("expressed_by_magic") is None


def test_lucene_query_escapes():
    q = lucene_query('RHO) OR id:"x" AND *')
    assert "\\)" in q and '\\"' in q and "\\:" in q
    assert lucene_query("Muller glia").startswith('"Muller glia"^4')


def test_fit_trims_and_warns():
    env = envelope("r", [{"x": "y" * 100} for _ in range(500)], page={"limit": 500, "offset": 0, "total": 500})
    out = fit(env, 5000)
    assert len(dumps(out)) <= 5000
    assert 0 < len(out["data"]) < 500
    assert any("returned" in w and "of 500" in w for w in out["warnings"])


def test_fit_dict_list():
    env = envelope("r", {"edge": {"a": 1}, "study_results": [{"t": "z" * 200} for _ in range(300)]})
    out = fit(env, 4000)
    assert len(dumps(out)) <= 4000 and out["data"]["edge"] == {"a": 1}
    assert any(w.startswith("data.study_results") for w in out["warnings"])


def test_tokens_parse():
    t = parse_tokens("# c\nabcdefghijklmnop\tread, proposals:read\n")
    assert list(t.values()) == [frozenset({"read", "proposals:read"})]
    with pytest.raises(ValueError):
        parse_tokens("short\tread\n")
    with pytest.raises(ValueError):
        parse_tokens("abcdefghijklmnop\tadmin\n")


def test_read_secret(tmp_path):
    p = tmp_path / "pw"
    p.write_text("s3cret\n")
    assert read_secret(p) == "s3cret"
    with pytest.raises(ConfigError):
        read_secret(tmp_path / "missing")


def test_settings_from_env_only_release_dir(tmp_path):
    s = Settings.from_env({"VISIONKB_DATA_ROOT": str(tmp_path), "VISIONKB_KG_RELEASE": "x"})
    assert s.release_dir is None  # v0.3: no fallback to the data root
    with pytest.raises(ConfigError):
        Settings.from_env({"VISIONKB_MAX_ROWS": "500"})


# -- release bundle ----------------------------------------------------------------------------
def test_release_loads(release_dir):
    rel = load_release(release_dir, "visionkb-0000.01.0", verify_checksums=True)
    assert rel.kg_release == "visionkb-0000.01.0"
    assert {d["id"] for d in rel.datasets} == {"visionkb:ds-hrca-snrna", "visionkb:ds-rpe-scrna"}
    assert rel.checks["sha256"] == "ok"
    with pytest.raises(ConfigError, match="VISIONKB_KG_RELEASE"):
        load_release(release_dir, "visionkb-2026.11.1")


def test_release_id_must_match_contract(release_dir, tmp_path):
    dst = tmp_path / "bad"
    shutil.copytree(release_dir, dst)
    (dst / "RELEASE").write_text("visionkb-2026.11.0-fixture\n")
    with pytest.raises(ConfigError, match=r"expected visionkb-YYYY\.MM\.N"):
        load_release(dst)


def _copy(release_dir, tmp_path) -> Path:
    dst = tmp_path / release_dir.name
    shutil.copytree(release_dir, dst)
    return dst


def test_release_rejects_paths_outside_bundle(release_dir, tmp_path):
    dst = _copy(release_dir, tmp_path)
    m = json.loads((dst / "manifest.json").read_text())
    m["files"].append({"path": "../outside.txt", "bytes": 1, "sha256": "0" * 64})
    (dst / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ConfigError, match="relative path inside the bundle"):
        load_release(dst)
    m["files"][-1]["path"] = "/etc/hostname"
    (dst / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ConfigError, match="relative path inside the bundle"):
        load_release(dst)


def test_release_detects_corruption(release_dir, tmp_path):
    dst = _copy(release_dir, tmp_path)
    reg = dst / "registry" / "datasets.jsonl"
    data = reg.read_bytes()
    reg.write_bytes(data.replace(b"lab", b"LAB", 1))  # same size, different bytes
    load_release(dst)  # sizes match
    with pytest.raises(ConfigError, match="checksum"):
        load_release(dst, verify_checksums=True)
    reg.write_bytes(data + b"\n")
    with pytest.raises(ConfigError, match="size"):
        load_release(dst)


def test_fixture_bundle(release_dir):
    tar = release_dir.parent / f"{release_dir.name}.tar"
    if not tar.exists():
        pytest.skip("bundle tar not next to the release dir")
    digest, name = (release_dir.parent / f"{tar.name}.sha256").read_text().split()
    import hashlib

    assert hashlib.sha256(tar.read_bytes()).hexdigest() == digest and name == tar.name
    with tarfile.open(tar) as tf:
        names = tf.getnames()
    assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)
    assert f"{release_dir.name}/manifest.json" in names
    m = json.loads((release_dir / "manifest.json").read_text())
    listed = {f["path"] for f in m["files"]}
    on_disk = {p.relative_to(release_dir).as_posix() for p in release_dir.rglob("*") if p.is_file()}
    assert on_disk - {"manifest.json"} == listed


# -- summaries (DuckDB) ------------------------------------------------------------------------
def test_summaries(release_dir):
    s = Summaries(release_dir / "summaries" / "expr_summary.parquet")
    found, missing = s.resolve_genes(["rho", "HGNC:16665", "NOPE1"])
    assert found["rho"] == [("HGNC:10012", "RHO")] and missing == ["NOPE1"]
    rows, total = s.expression(
        ["HGNC:10012"], dataset=None, level="majorclass", cell_type=None, sort="pseudobulk_cp10k", limit=50, offset=0
    )
    assert total == 6 and rows[0]["group_label"] == "Rod" and rows[0]["dataset_version"] == "heca-2026.05"
    assert rows == sorted(rows, key=lambda r: -r["pseudobulk_cp10k"])
    rows, total = s.expression(
        ["HGNC:710"], dataset=None, level="celltype", cell_type="s_cone", sort="pct_expressing", limit=5, offset=0
    )
    assert total == 1 and rows[0]["cell_type_id"] == "visionkb:cs-hrca-snrna-s-cone"
    assert s.datasets_expressing("HGNC:16665") == {"visionkb:ds-hrca-snrna": 0.333333, "visionkb:ds-rpe-scrna": 0.34}


def test_specificity_ranking(release_dir):
    """Rod (hrca-snrna majorclass): ranking by specificity differs from pct; scores recomputed from the parquet."""
    import duckdb

    pq = str(release_dir / "summaries" / "expr_summary.parquet")
    q = (
        "SELECT gene_id, gene_symbol, group_label, pseudobulk_cp10k, pct_expressing FROM read_parquet(?) "
        "WHERE dataset_id = 'visionkb:ds-hrca-snrna' AND level = 'majorclass'"
    )
    all_rows = duckdb.connect().execute(q, [pq]).fetchall()
    cands = [
        {
            "gene_id": g,
            "symbol": s,
            "name": s,
            "edge_id": f"e-{s}",
            "target": "CL:0000604",
            "dataset_id": "visionkb:ds-hrca-snrna",
            "level": "majorclass",
            "group_label": lab,
            "pseudobulk_cp10k": pb,
            "pct_expressing": pct,
        }
        for g, s, lab, pb, pct in all_rows
        if lab == "Rod" and s in ("RHO", "GNAT1", "ABCA4")
    ]
    summ = Summaries(release_dir / "summaries" / "expr_summary.parquet")
    items, total = rank_by_specificity(cands, summ.other_group_max([c["gene_id"] for c in cands]), 50, 0)
    assert total == 3 and [i["symbol"] for i in items] == ["GNAT1", "RHO", "ABCA4"]
    assert sorted(cands, key=lambda c: -c["pct_expressing"])[0]["symbol"] == "RHO"  # pct order differs
    for i in items:
        mine = {lab: pb for g, s, lab, pb, _ in all_rows if s == i["symbol"]}
        other = max(v for lab, v in mine.items() if lab != "Rod")
        assert i["score"] == round((mine["Rod"] + PSEUDOCOUNT) / (other + PSEUDOCOUNT), 6)
        assert i["best_study_result"]["other_max_pseudobulk_cp10k"] == round(other, 6)
        assert i["best_study_result"]["other_max_group"] == max((v, lab) for lab, v in mine.items() if lab != "Rod")[1]


# -- HTTP without databases --------------------------------------------------------------------
def test_health_without_databases(client):
    body = ok(client.get("/v1/health"))
    d = body["data"]
    assert d["release"] == "visionkb-0000.01.0"
    assert d["summaries"]["status"] == "ok" and d["summaries"]["n_rows"] == 80
    assert d["neo4j"]["status"] == "not_configured" and d["postgres"]["status"] == "not_configured"
    assert any(w.startswith("neo4j") for w in body["warnings"])


def test_ready_without_databases(client):
    r = client.get("/v1/ready")
    assert r.status_code == 503, r.text
    b = r.json()
    assert b["status"] == "error" and b["kg_release"] == "visionkb-0000.01.0"
    assert b["error"]["code"] == "not_ready" and b["error"]["message"] == "not ready: neo4j, postgres"
    assert b["data"]["summaries"]["status"] == "ok" and b["data"]["neo4j"]["status"] == "not_configured"
    assert "neo4j: not_configured" in b["warnings"] and "postgres: not_configured" in b["warnings"]
    # health stays 200 for people and monitoring
    assert client.get("/v1/health").status_code == 200


def test_auth_required(client):
    err(client.get("/v1/release"), 401, "unauthorized")
    err(client.get("/v1/release", headers={"Authorization": "Bearer wrong-token-000000000"}), 401, "unauthorized")
    err(client.get("/v1/release", headers=auth("pw")), 403, "forbidden")
    err(client.get("/v1/proposals", headers=auth("read")), 403, "forbidden")


def test_release_endpoint(client):
    d = ok(client.get("/v1/release", headers=auth()))["data"]
    assert d["biolink_version"] == "4.4.6" and d["n_datasets"] == 2
    assert d["counts"]["edges_by_predicate"]["biolink:gene_associated_with_condition"] == 2
    assert all("path_or_url" not in s for s in d["sources"])
    assert [ki["id"] for ki in d["known_issues"]] == ["KI-1"] and d["n_known_issue_groups"] == 1
    assert d["expression_edge_rule"]["decision"] == "D18" and "D18" in d["expression_edge_rule_text"]


def test_edge_rule_text_from_manifest_and_fallback(release_dir, tmp_path):
    rel = load_release(release_dir)
    text = rel.edge_rule_text()
    assert text == (
        "pct_expressing >= 0.1 and n_cells >= 20; pseudobulk_cp10k >= 0.1 x the gene's max in the same dataset and "
        "level; n_donors_expressing >= 2 (where donor data exist); levels majorclass, celltype only (decision D18)"
    )
    assert rel.min_pct_for_edges == 0.1
    dst = _copy(release_dir, tmp_path)
    m = json.loads((dst / "manifest.json").read_text())
    del m["expression_edge_rule"]
    (dst / "manifest.json").write_text(json.dumps(m))
    old = load_release(dst)
    assert old.edge_rule_text() == (
        "pct_expressing >= 0.1 and n_cells >= 20 (decision D8; this release's manifest has no expression_edge_rule)"
    )


def test_known_issue_groups(release_dir, tmp_path):
    rel = load_release(release_dir)
    mg = {"id": "KI-1", "program_of": "Rod", "group_majorclass": "MG"}
    assert rel.known_issue_groups == {("visionkb:ds-hrca-snrna", "MG"): [mg]}
    ds = "visionkb:ds-hrca-snrna"
    assert rel.known_issues_for(ds, "MG", "celltype") == [mg] and rel.known_issues_for(ds, "MG", "majorclass") == [mg]
    assert (
        rel.known_issues_for(ds, "MG", "region_majorclass") == []
        and rel.known_issues_for(ds, "Rod", "majorclass") == []
    )
    # a flagged cell-type group whose label is also a different major class: majorclass rows are not tagged
    rel.known_issue_groups[(ds, "Cone")] = [{"id": "KI-1", "program_of": "Rod", "group_majorclass": "PR"}]
    assert rel.known_issues_for(ds, "Cone", "celltype") and rel.known_issues_for(ds, "Cone", "majorclass") == []
    # details file unreadable -> fall back to the manifest's *_groups list, and say so in the bundle checks
    dst = _copy(release_dir, tmp_path)
    m = json.loads((dst / "manifest.json").read_text())
    m["known_issues"][0]["details"] = "validation/missing.json"
    (dst / "manifest.json").write_text(json.dumps(m))
    rel2 = load_release(dst)
    assert rel2.known_issue_groups == {("visionkb:ds-hrca-snrna", "MG"): [{"id": "KI-1", "program_of": "Rod"}]}
    # fallback list has no group_majorclass: only cell-type (or unknown-level) rows are tagged
    assert rel2.known_issues_for("visionkb:ds-hrca-snrna", "MG", "majorclass") == []
    assert "validation/missing.json" in rel2.checks["known_issues"][0]
    # no known_issues in the manifest -> nothing flagged
    del m["known_issues"]
    (dst / "manifest.json").write_text(json.dumps(m))
    assert load_release(dst).known_issue_groups == {}


def test_atlas_expression(client):
    body = ok(client.get("/v1/atlas/expression", params={"genes": "RHO,apln", "level": "majorclass"}, headers=auth()))
    assert body["page"] == {"limit": 50, "offset": 0, "total": 12}
    assert body["data"][0]["gene_symbol"] == "RHO" and body["data"][0]["group_label"] == "Rod"
    flagged = [r for r in body["data"] if r.get("known_issues")]
    assert {(r["gene_symbol"], r["group_label"]) for r in flagged} == {("RHO", "MG"), ("APLN", "MG")}
    assert all(r["known_issues"] == ["KI-1"] for r in flagged)
    assert any(w.startswith("KI-1 (") and "2 of 12 returned rows" in w for w in body["warnings"])
    body = ok(
        client.get(
            "/v1/atlas/expression",
            params=[("genes", "PECAM1"), ("genes", "XYZ9"), ("dataset", "visionkb:ds-rpe-scrna")],
            headers=auth(),
        )
    )
    assert [r["group_label"] for r in body["data"]] == ["Endo", "RPE"]
    assert any("XYZ9" in w for w in body["warnings"])
    err(
        client.get("/v1/atlas/expression", params={"genes": ",".join(f"G{i}" for i in range(21))}, headers=auth()),
        400,
        "too_many_genes",
    )
    err(
        client.get("/v1/atlas/expression", params={"genes": "RHO", "level": "bogus"}, headers=auth()),
        422,
        "invalid_request",
    )
    err(
        client.get("/v1/atlas/expression", params={"genes": "RHO", "sort": "gene_id; DROP"}, headers=auth()),
        422,
        "invalid_request",
    )


def test_limit_capped(client):
    body = ok(client.get("/v1/atlas/expression", params={"genes": "RHO", "limit": 500}, headers=auth()))
    assert body["page"]["limit"] == 200 and any("capped at 200" in w for w in body["warnings"])


def test_char_budget(release_dir, tokens_file):
    s = Settings(release_dir=release_dir, tokens_file=tokens_file, max_chars=3000)
    with TestClient(create_app(s)) as c:
        r = c.get(
            "/v1/atlas/expression", params={"genes": "RHO,GNAT1,ARR3,ABCA4", "level": "majorclass"}, headers=auth()
        )
    body = ok(r)
    assert len(r.text) <= 3000
    assert body["page"]["total"] == 24 and len(body["data"]) < 24
    assert any("returned" in w and "of 24" in w for w in body["warnings"])


def test_datasets(client):
    body = ok(client.get("/v1/datasets", headers=auth()))
    assert body["page"]["total"] == 2
    assert all(d["bind"]["role"] == "reference" and d["bind"]["path"] for d in body["data"])
    body = ok(client.get("/v1/datasets", params={"entities": "CL:0002586"}, headers=auth()))
    assert [d["id"] for d in body["data"]] == ["visionkb:ds-rpe-scrna"]
    assert body["data"][0]["matched"] == [{"entity": "CL:0002586", "field": "cell_types"}]
    body = ok(client.get("/v1/datasets", params={"entities": "HGNC:10294"}, headers=auth()))  # RPE65
    assert [d["id"] for d in body["data"]] == ["visionkb:ds-rpe-scrna"]
    assert body["data"][0]["matched"][0]["field"] == "expression"
    body = ok(client.get("/v1/datasets", params={"assay": "EFO:0009809", "tissue": "UBERON:0000966"}, headers=auth()))
    assert [d["id"] for d in body["data"]] == ["visionkb:ds-hrca-snrna"]
    assert body["data"][0]["matched_tissue"] == ["UBERON:0000966"]
    assert "tissue not expanded: graph not configured" in body["warnings"]
    err(client.get("/v1/datasets", params={"entities": "not a curie"}, headers=auth()), 400, "invalid_curie")


def test_graph_endpoints_without_graph(client):
    err(client.get("/v1/entities/search", params={"q": "RHO"}, headers=auth()), 503, "neo4j_unavailable")
    err(client.get("/v1/relationships", params={"subject": "HGNC:10012"}, headers=auth()), 503, "neo4j_unavailable")
    err(client.get("/v1/relationships", headers=auth()), 400, "missing_anchor")
    err(client.get("/v1/evidence/visionkb:e-123", headers=auth()), 400, "invalid_edge_id")
    err(client.get("/v1/gene-sets", params={"entity": "HGNC:10012"}, headers=auth()), 400, "invalid_entity")


GOOD = {
    "edge": {
        "subject": "HGNC:16665",
        "predicate": "biolink:expressed_in",
        "object": "CL:0000115",
        "knowledge_level": "observation",
        "agent_type": "data_analysis_pipeline",
        "anatomical_context_qualifier": "UBERON:0000966",
        "publications": ["PMID:2137202"],
    },
    "evidence": [{"dataset_id": "visionkb:ds-hrca-snrna", "pct_expressing": 0.33}],
    "run_id": "unit-run-1",
    "visibility": "lab",
}


@pytest.mark.parametrize(
    "patch,needle",
    [
        ({"predicate": "biolink:is_friends_with"}, "not a Biolink 4.4.6 predicate"),
        ({"predicate": "biolink:interacts_with"}, "mixin"),
        ({"subject": "RHO"}, "not a CURIE"),
        ({"knowledge_level": "vibes"}, "knowledge_level"),
        ({"primary_knowledge_source": "visionkb"}, "infores"),
        ({"publications": ["https://example.org"]}, "PMID"),
        ({"made_up_qualifier": "x"}, "Extra inputs"),
    ],
)
def test_proposal_validation(client, patch, needle):
    body = json.loads(json.dumps(GOOD))
    body["edge"].update(patch)
    r = client.post("/v1/proposals", json=body, headers=auth("pw"))
    e = err(r, 422, "invalid_request")
    assert needle in e["error"]["message"]


def test_proposal_scope_and_store(client):
    err(client.post("/v1/proposals", json=GOOD, headers=auth("read")), 403, "forbidden")
    err(client.post("/v1/proposals", json=GOOD, headers=auth("pw")), 503, "postgres_unavailable")
    big = dict(GOOD, evidence=[{"x": "y" * 3000}] * 30)
    err(client.post("/v1/proposals", json=big, headers=auth("pw")), 413, "too_large")


def test_unknown_route(client):
    err(client.get("/v1/nope", headers=auth()), 404, "not_found")


def test_openapi_in_sync():
    spec = create_app().openapi()
    on_disk = json.loads((ROOT / "docs" / "openapi.json").read_text())
    assert spec == on_disk, "run: visionkb-api openapi --out docs/openapi.json"
    assert set(spec["paths"]) >= {
        "/v1/health",
        "/v1/release",
        "/v1/entities/search",
        "/v1/entities/{curie}",
        "/v1/relationships",
        "/v1/evidence/{edge_id}",
        "/v1/gene-sets",
        "/v1/atlas/expression",
        "/v1/datasets",
        "/v1/proposals",
    }


def test_tokens_constant(tokens_file):
    assert TOKENS["read"] in tokens_file.read_text()
