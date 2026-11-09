"""Integration tests: live Neo4j (fixture imported with neo4j-admin + post_import.cypher) and PostgreSQL.

Enable with VISIONKB_IT=1 and: NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD_FILE, VISIONKB_PG_DSN_FILE,
VISIONKB_TEST_RELEASE_DIR (the same fixture release that was imported).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from visionkb_api.app import create_app
from visionkb_api.config import Settings, read_secret

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("VISIONKB_IT") != "1", reason="set VISIONKB_IT=1 with live Neo4j/PostgreSQL"),
]
REL = "visionkb-2026.11.0-fixture"
RHO, APLN, ABCA4, ARR3, PECAM1 = "HGNC:10012", "HGNC:16665", "HGNC:34", "HGNC:710", "HGNC:8823"
ROD, CONE, MG, RP, RP4 = "CL:0000604", "CL:0000573", "CL:0000636", "MONDO:0019200", "MONDO:0013395"


@pytest.fixture(scope="module")
def client(release_dir, tokens_file):
    s = Settings(
        release_dir=Path(release_dir),
        kg_release=REL,
        neo4j_uri=os.environ["NEO4J_URI"],
        neo4j_user=os.environ.get("NEO4J_USER", "neo4j"),
        neo4j_password_file=Path(os.environ["NEO4J_PASSWORD_FILE"]),
        pg_dsn_file=Path(os.environ["VISIONKB_PG_DSN_FILE"]),
        tokens_file=tokens_file,
    )
    with TestClient(create_app(s)) as c:
        yield c


def graph_counts() -> tuple[int, int]:
    import neo4j

    pw = read_secret(os.environ["NEO4J_PASSWORD_FILE"])
    with neo4j.GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ.get("NEO4J_USER", "neo4j"), pw)) as d:
        rec = d.execute_query("MATCH (n) WITH count(n) AS n MATCH ()-[r]->() RETURN n, count(r) AS r").records[0]
        return rec["n"], rec["r"]


def ok(r, code=200):
    assert r.status_code == code, r.text
    b = r.json()
    assert b["status"] == "ok" and b["kg_release"] == REL, b
    return b


def err(r, code, ecode):
    assert r.status_code == code, r.text
    b = r.json()
    assert b["status"] == "error" and b["error"]["code"] == ecode and b["kg_release"] == REL, b
    return b


def test_health(client):
    d = ok(client.get("/v1/health"))["data"]
    assert d["neo4j"]["status"] == "ok" and d["neo4j"]["kg_release"] == REL, d
    assert d["neo4j"]["edition"] == "community"
    assert d["postgres"]["status"] == "ok" and d["postgres"]["schema_version"] == 1
    assert d["summaries"]["status"] == "ok"


def test_release_predicates(client):
    d = ok(client.get("/v1/release", headers=auth()))["data"]
    assert d["predicates_in_graph"] == [
        "biolink:expressed_in",
        "biolink:gene_associated_with_condition",
        "biolink:subclass_of",
    ]


@pytest.mark.parametrize(
    "q,first_id,match",
    [
        ("HGNC:16665", APLN, "exact_id"),
        ("RHO", RHO, "exact_symbol"),
        ("rho", RHO, "exact_symbol"),
        ("CD31", PECAM1, "synonym"),
        ("Muller glia", MG, "synonym"),
        ("retinal rod cell", ROD, "fulltext"),
    ],
)
def test_search(client, q, first_id, match):
    b = ok(client.get("/v1/entities/search", params={"q": q}, headers=auth()))
    assert b["data"][0]["id"] == first_id and b["data"][0]["match"] == match, b["data"][:3]


def test_search_category_and_fulltext(client):
    b = ok(client.get("/v1/entities/search", params={"q": "retinitis", "category": "biolink:Disease"}, headers=auth()))
    ids = [h["id"] for h in b["data"]]
    assert set(ids) == {RP, RP4} and all(h["match"] == "fulltext" for h in b["data"])
    err(
        client.get("/v1/entities/search", params={"q": "x", "category": "biolink:Spaceship"}, headers=auth()),
        400,
        "invalid_category",
    )


@pytest.mark.parametrize("q", ["RHO)", '"', "x' }) MATCH (n) DETACH DELETE n //", "*:*", "AND OR NOT", "\\"])
def test_search_hostile_input(client, q):
    before = graph_counts()
    ok(client.get("/v1/entities/search", params={"q": q}, headers=auth()))
    assert graph_counts() == before


def test_entity(client):
    d = ok(client.get(f"/v1/entities/{RHO}", headers=auth()))["data"]
    assert d["node"]["symbol"] == "RHO" and "ENSEMBL:ENSG00000163914" in d["node"]["xref"]
    # Rod, Cone and the two cone CellStates (fixture: pct >= 0.10, n_cells >= 20)
    assert d["degree_by_predicate"]["biolink:expressed_in"]["out"] == 4
    assert d["degree_by_predicate"]["biolink:gene_associated_with_condition"]["out"] == 1
    err(client.get("/v1/entities/HGNC:99999999", headers=auth()), 400, "invalid_curie")
    err(client.get("/v1/entities/HGNC:999999", headers=auth()), 404, "not_found")


def test_relationships(client):
    b = ok(client.get("/v1/relationships", params={"subject": RHO}, headers=auth()))
    assert b["page"]["total"] == 5
    preds = sorted(e["predicate"] for e in b["data"])
    assert preds == ["biolink:expressed_in"] * 4 + ["biolink:gene_associated_with_condition"]
    e = next(e for e in b["data"] if e["object"]["id"] == ROD)
    assert e["qualifiers"] == {
        "anatomical_context_qualifier": "UBERON:0000966",
        "species_context_qualifier": "NCBITaxon:9606",
    }
    assert e["knowledge_level"] == "observation" and e["primary_knowledge_source"] == "infores:visionkb-heca"
    assert e["summary"]["max_pct_expressing"] == 0.95 and e["edge_id"].startswith("visionkb:e-")
    b = ok(
        client.get("/v1/relationships", params={"subject": RHO, "object_category": "biolink:Disease"}, headers=auth())
    )
    assert [x["object"]["id"] for x in b["data"]] == [RP4]
    assert b["data"][0]["publications"] == ["PMID:2137202"]
    b = ok(
        client.get("/v1/relationships", params={"object": ROD, "predicate": "expressed_in", "limit": 2}, headers=auth())
    )
    assert b["page"] == {"limit": 2, "offset": 0, "total": 3} and len(b["data"]) == 2
    b2 = ok(
        client.get(
            "/v1/relationships",
            params={"object": ROD, "predicate": "expressed_in", "limit": 2, "offset": 2},
            headers=auth(),
        )
    )
    assert {x["edge_id"] for x in b["data"]}.isdisjoint({x["edge_id"] for x in b2["data"]})
    b = ok(
        client.get(
            "/v1/relationships", params={"subject": RHO, "knowledge_level": "knowledge_assertion"}, headers=auth()
        )
    )
    assert len(b["data"]) == 1
    b = ok(client.get("/v1/relationships", params={"subject": RHO, "species": "NCBITaxon:10090"}, headers=auth()))
    assert b["page"]["total"] == 0


def test_relationship_predicate_checks(client):
    err(
        client.get("/v1/relationships", params={"subject": RHO, "predicate": "biolink:treats"}, headers=auth()),
        400,
        "predicate_not_in_release",
    )
    err(
        client.get(
            "/v1/relationships", params={"subject": RHO, "predicate": "EXPRESSED_IN`]->() DELETE r//"}, headers=auth()
        ),
        400,
        "unknown_predicate",
    )
    err(
        client.get("/v1/relationships", params={"subject": RHO, "knowledge_level": "rumor"}, headers=auth()),
        400,
        "invalid_knowledge_level",
    )


def test_evidence(client):
    rels = ok(client.get("/v1/relationships", params={"subject": RHO, "object": ROD}, headers=auth()))["data"]
    eid = rels[0]["edge_id"]
    d = ok(client.get(f"/v1/evidence/{eid}", headers=auth()))
    assert d["page"]["total"] == 1
    sr = d["data"]["study_results"][0]
    assert sr["dataset_id"] == "visionkb:ds-hrca-snrna" and sr["group_label"] == "Rod" and sr["pct_expressing"] == 0.95
    assert sr["dataset_name"].startswith("SYNTHETIC FIXTURE")
    rels = ok(client.get("/v1/relationships", params={"subject": RHO, "object": RP4}, headers=auth()))["data"]
    d = ok(client.get(f"/v1/evidence/{rels[0]['edge_id']}", headers=auth()))["data"]
    assert d["publications"] == ["PMID:2137202"] and d["study_results"][0]["pmid"] == "PMID:2137202"
    assert len(d["study_results"][0]["supporting_text"]) <= 300
    err(client.get("/v1/evidence/visionkb:e-" + "0" * 40, headers=auth()), 404, "not_found")


def test_gene_sets(client):
    b = ok(client.get("/v1/gene-sets", params={"entity": ROD}, headers=auth()))
    assert [g["symbol"] for g in b["data"]] == ["RHO", "GNAT1", "ABCA4"]
    assert b["data"][0]["score"] == 0.95 and b["data"][0]["evidence"][0]["edge_id"].startswith("visionkb:e-")
    # Cone includes its CellStates (subclass_of): ARR3 is top.
    b = ok(client.get("/v1/gene-sets", params={"entity": CONE, "kind": "expressed"}, headers=auth()))
    assert b["data"][0]["symbol"] == "ARR3"
    targets = {e["target"] for g in b["data"] for e in g["evidence"]}
    assert "visionkb:cs-hrca-snrna-s-cone" in targets
    b = ok(client.get("/v1/gene-sets", params={"entity": CONE, "descendants": "false"}, headers=auth()))
    assert {e["target"] for g in b["data"] for e in g["evidence"]} == {CONE}
    b = ok(client.get("/v1/gene-sets", params={"entity": ROD, "min_pct": 0.8}, headers=auth()))
    assert [g["symbol"] for g in b["data"]] == ["RHO", "GNAT1"]
    b = ok(client.get("/v1/gene-sets", params={"entity": ROD, "dataset": "visionkb:ds-rpe-scrna"}, headers=auth()))
    assert b["data"] == []
    b = ok(client.get("/v1/gene-sets", params={"entity": "CL:0000115", "min_pct": 0.05}, headers=auth()))
    assert any("lower thresholds" in w for w in b["warnings"])
    assert {g["symbol"] for g in b["data"]} == {"APLN", "PECAM1"}  # rpe Endo only: hrca Endo has 15 cells < 20
    # Disease genes, through MONDO descendants: RP -> RP4 -> RHO.
    b = ok(client.get("/v1/gene-sets", params={"entity": RP}, headers=auth()))
    assert [g["gene_id"] for g in b["data"]] == [RHO]
    b = ok(client.get("/v1/gene-sets", params={"entity": RP, "descendants": "false"}, headers=auth()))
    assert b["data"] == []
    err(client.get("/v1/gene-sets", params={"entity": "HP:0009999"}, headers=auth()), 404, "not_found")


def test_datasets_with_graph(client):
    b = ok(client.get("/v1/datasets", params={"entities": [APLN, "CL:0000115"]}, headers=auth()))
    assert {d["id"] for d in b["data"]} == {"visionkb:ds-hrca-snrna", "visionkb:ds-rpe-scrna"}
    assert all(d["score"] == 1.0 for d in b["data"])


def test_proposals_roundtrip(client):
    before = graph_counts()
    run = "it-" + uuid.uuid4().hex[:12]
    body = {
        "edge": {
            "subject": APLN,
            "predicate": "biolink:expressed_in",
            "object": "CL:0000115",
            "knowledge_level": "observation",
            "agent_type": "data_analysis_pipeline",
            "anatomical_context_qualifier": "UBERON:0000966",
        },
        "evidence": [{"dataset_id": "visionkb:ds-hrca-snrna", "group_label": "Endo", "note": "integration test"}],
        "run_id": run,
        "visibility": "lab",
    }
    r = client.post("/v1/proposals", json=body, headers=auth("pw"))
    d = ok(r, 201)["data"]
    assert d["status"] == "proposed" and d["created"] is True and d["proposal_id"].startswith("visionkb:p-")
    again = ok(client.post("/v1/proposals", json=body, headers=auth("pw")), 200)
    assert again["data"]["proposal_id"] == d["proposal_id"] and again["data"]["created"] is False
    unknown = dict(body, edge=dict(body["edge"], subject="HGNC:999999"))
    w = ok(client.post("/v1/proposals", json=unknown, headers=auth("pw")), 201)["warnings"]
    assert any("HGNC:999999 is not in" in x for x in w)
    lst = ok(client.get("/v1/proposals", params={"status": "proposed", "limit": 200}, headers=auth("pr")))
    mine = [p for p in lst["data"] if p["run_id"] == run]
    assert len(mine) == 2 and all(p["kg_release"] == REL and p["status"] == "proposed" for p in mine)
    assert mine[-1]["edge"]["anatomical_context_qualifier"] == "UBERON:0000966"
    assert all(p["submitted_by"].startswith("t-") for p in mine)
    err(client.get("/v1/proposals", headers=auth("pw")), 403, "forbidden")
    assert graph_counts() == before  # proposals never touch Neo4j
