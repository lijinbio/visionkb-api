"""Neo4j access (contracts §5). Every query is a fixed Cypher text with parameters.

User input only ever reaches Cypher as a parameter. Relationship types are passed as parameters
too (``[r:$any($types)]``), after checking them against the Biolink predicates present in the
release, so no identifier is spliced into a query string.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from .envelope import ApiError
from .vocab import biolink, looks_like_curie

EXPRESSED_IN = "expressed_in"
SUBCLASS_OF = "subclass_of"
DISEASE_GENE_PREDICATES = (
    "causes",
    "contributes_to",
    "gene_associated_with_condition",
    "genetically_associated_with",
    "has_phenotype",
    "associated_with",
)
SUMMARY_PROPS = ("max_pct_expressing", "max_mean_log1p_cp10k", "n_datasets")
EDGE_CORE = {
    "id",
    "predicate",
    "knowledge_level",
    "agent_type",
    "primary_knowledge_source",
    "aggregator_knowledge_source",
    "publications",
    "has_study_results",
    "evidence_count",
    "status",
    "visibility",
    "kg_release",
    *SUMMARY_PROPS,
}
MAX_PUBS_PER_EDGE = 5
_LUCENE_TOKEN = re.compile(r"\w+", re.UNICODE)
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
NOT_REJECTED = "coalesce(r.status, 'curated') <> 'rejected'"


def lucene_query(q: str) -> str:
    """Escaped Lucene query: exact phrase boosted, then all terms, then all term prefixes."""
    phrase = _LUCENE_SPECIAL.sub(r"\\\1", q.strip())
    toks = [t.lower() for t in _LUCENE_TOKEN.findall(q)][:8]
    parts = [f'"{phrase}"^4']
    if toks:
        parts.append("(" + " AND ".join(toks) + ")^2")
        parts.append("(" + " AND ".join(f"{t}*" for t in toks) + ")")
    return " OR ".join(parts)


def _plain(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return str(v)


def node_dict(node: Any) -> dict:
    return {k: _plain(v) for k, v in dict(node).items()}


def edge_dict(r: Any, rtype: str, s: dict, o: dict) -> dict:
    props = {k: _plain(v) for k, v in dict(r).items()}
    quals = {k: v for k, v in props.items() if k.endswith("_qualifier") or k == "qualified_predicate"}
    pubs = props.get("publications") or []
    out = {
        "edge_id": props.get("id"),
        "subject": s,
        "predicate": props.get("predicate") or f"biolink:{rtype}",
        "object": o,
        "qualifiers": quals,
        "knowledge_level": props.get("knowledge_level"),
        "agent_type": props.get("agent_type"),
        "primary_knowledge_source": props.get("primary_knowledge_source"),
        "aggregator_knowledge_source": props.get("aggregator_knowledge_source") or [],
        "publications": pubs[:MAX_PUBS_PER_EDGE],
        "n_publications": len(pubs),
        "evidence_count": props.get("evidence_count"),
        "status": props.get("status"),
        "visibility": props.get("visibility"),
    }
    summary = {k: props[k] for k in SUMMARY_PROPS if props.get(k) is not None}
    if summary:
        out["summary"] = summary
    return out


_REL_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,80}$")


def _rel_type(t: str) -> str:
    """Backtick-quoted relationship type, only for Biolink predicate names (allowlist + pattern)."""
    if not _REL_TYPE_RE.match(t) or f"biolink:{t}" not in biolink()["predicates"]:
        raise ApiError(500, "graph_error", "unexpected relationship type")
    return f"`{t}`"


def _brief(props: dict) -> dict:
    return {"id": props.get("id"), "name": props.get("name"), "category": props.get("category") or []}


class Graph:
    """Thin wrapper over the Neo4j driver: fixed read queries, timeouts, error mapping."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j", timeout_s: float = 15.0):
        import neo4j

        self._neo4j = neo4j
        self.database = database
        self.timeout_s = timeout_s
        self.driver = neo4j.GraphDatabase.driver(
            uri, auth=(user, password), connection_timeout=5.0, max_transaction_retry_time=5.0
        )
        self._types: set[str] | None = None
        self._types_at = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        self.driver.close()

    # -- low level -----------------------------------------------------------------------------
    def run(self, text: str, **params: Any) -> list:
        neo4j = self._neo4j
        try:
            with self.driver.session(database=self.database, default_access_mode=neo4j.READ_ACCESS) as s:
                return list(s.run(neo4j.Query(text, timeout=self.timeout_s), params))
        except neo4j.exceptions.ServiceUnavailable:
            raise ApiError(503, "neo4j_unavailable", "the graph database is not reachable") from None
        except neo4j.exceptions.AuthError:
            raise ApiError(503, "neo4j_unavailable", "the graph database rejected the API credentials") from None
        except neo4j.exceptions.ClientError as exc:
            code = getattr(exc, "code", "") or ""
            if "Timeout" in code or "TransactionTimedOut" in code:
                raise ApiError(504, "query_timeout", "the graph query took too long; narrow it") from None
            raise ApiError(500, "graph_error", f"graph query failed ({code})") from None

    def rel_types(self) -> set[str]:
        """Relationship types in the database (cached for 5 minutes)."""
        with self._lock:
            if self._types is None or time.monotonic() - self._types_at > 300:
                rows = self.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")
                self._types = {r[0] for r in rows}
                self._types_at = time.monotonic()
            return set(self._types)

    def predicate_types(self) -> set[str]:
        """Relationship types that are Biolink predicates (excludes structural ``supporting_data_set``)."""
        preds = biolink()["predicates"]
        return {t for t in self.rel_types() if f"biolink:{t}" in preds}

    def ping(self) -> dict:
        rows = self.run("CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition")
        info = (
            {"name": rows[0]["name"], "version": rows[0]["versions"][0], "edition": rows[0]["edition"]} if rows else {}
        )
        rel = self.run("MATCH ()-[r]->() WHERE r.kg_release IS NOT NULL RETURN r.kg_release AS rel LIMIT 1")
        info["kg_release"] = rel[0]["rel"] if rel else None
        return info

    # -- entities ------------------------------------------------------------------------------
    def search(self, q: str, category: str | None, limit: int) -> list[dict]:
        ql = q.strip().lower()
        hits: dict[str, dict] = {}

        def add(props: dict, match: str, score: float) -> None:
            nid = props.get("id")
            if not nid or nid in hits:
                return
            if category and category not in (props.get("category") or []):
                return
            hits[nid] = {
                "id": nid,
                "name": props.get("name"),
                "category": props.get("category") or [],
                "symbol": props.get("symbol"),
                "synonyms": (props.get("synonym") or [])[:10],
                "match": match,
                "score": round(score, 4),
            }

        if looks_like_curie(q.strip()):
            for r in self.run("MATCH (n:NamedThing {id: $id}) RETURN n", id=q.strip()):
                add(node_dict(r["n"]), "exact_id", 1.0)
        variants = sorted({q.strip(), q.strip().upper(), ql, q.strip().capitalize()})
        for r in self.run("MATCH (n:Gene) WHERE n.symbol IN $syms RETURN n LIMIT 20", syms=variants):
            props = node_dict(r["n"])
            if (props.get("symbol") or "").lower() == ql:
                add(props, "exact_symbol", 0.99)
        k = min(max(limit * 4, 50), 800)
        rows = self.run(
            # Cypher 25 SEARCH over the full-text index (db.index.fulltext.queryNodes is deprecated).
            "CYPHER 25 MATCH (node:NamedThing) SEARCH node IN (FULLTEXT INDEX entity_search FOR $lq LIMIT $k) "
            "SCORE AS score WITH node, score WHERE ($category IS NULL OR $category IN node.category) "
            "AND (NOT node:StudyResult OR $category = 'biolink:StudyResult') "
            "RETURN node, score ORDER BY score DESC",
            lq=lucene_query(q),
            k=k,
            category=category,
        )
        top = max((r["score"] for r in rows), default=1.0) or 1.0
        syn_hits, name_hits, rest = [], [], []
        for r in rows:
            props = node_dict(r["node"])
            if (props.get("symbol") or "").lower() == ql:
                add(props, "exact_symbol", 0.99)
            elif ql in {s.lower() for s in props.get("synonym") or []}:
                syn_hits.append(props)
            elif (props.get("name") or "").lower() == ql:
                name_hits.append(props)
            else:
                rest.append((props, r["score"]))
        for p in syn_hits:
            add(p, "synonym", 0.95)
        for p in name_hits:
            add(p, "fulltext", 0.9)
        for p, s in rest:
            add(p, "fulltext", 0.85 * s / top)
        return list(hits.values())[:limit]

    def get_node(self, curie: str) -> dict | None:
        rows = self.run("MATCH (n:NamedThing {id: $id}) RETURN n", id=curie)
        if not rows:
            return None
        node = node_dict(rows[0]["n"])
        out_deg = self.run(
            "MATCH (n:NamedThing {id: $id})-[r]->() RETURN type(r) AS t, count(r) AS c ORDER BY t", id=curie
        )
        in_deg = self.run(
            "MATCH (n:NamedThing {id: $id})<-[r]-() RETURN type(r) AS t, count(r) AS c ORDER BY t", id=curie
        )
        degree: dict[str, dict[str, int]] = {}
        for rows_, key in ((out_deg, "out"), (in_deg, "in")):
            for r in rows_:
                degree.setdefault(f"biolink:{r['t']}", {"out": 0, "in": 0})[key] = r["c"]
        return {"node": node, "degree_by_predicate": degree}

    def exists(self, ids: list[str]) -> dict[str, bool]:
        rows = self.run("UNWIND $ids AS i OPTIONAL MATCH (n:NamedThing {id: i}) RETURN i, n IS NOT NULL AS ok", ids=ids)
        return {r["i"]: r["ok"] for r in rows}

    def cell_states_under(self, curie: str) -> list[str]:
        if SUBCLASS_OF not in self.rel_types():
            return []
        rows = self.run(
            "MATCH (c:CellState)-[:subclass_of]->(:NamedThing {id: $id}) RETURN c.id AS id LIMIT 500", id=curie
        )
        return [r["id"] for r in rows]

    # -- relationships -------------------------------------------------------------------------
    def relationships(
        self,
        *,
        subject: str | None,
        object: str | None,
        types: list[str],
        object_category: str | None,
        knowledge_level: str | None,
        species: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        # Query text is chosen from fixed fragments; every value is a parameter.
        if subject:
            match = "MATCH (s:NamedThing {id: $subject})-[r:$any($types)]->(o:NamedThing)"
            if object:
                match += " WHERE o.id = $object"
        else:
            match = "MATCH (s:NamedThing)-[r:$any($types)]->(o:NamedThing {id: $object})"
        conds = [NOT_REJECTED]
        if object_category:
            conds.append("$object_category IN o.category")
        if knowledge_level:
            conds.append("r.knowledge_level = $knowledge_level")
        if species:
            conds.append(
                "(r.species_context_qualifier = $species OR (r.species_context_qualifier IS NULL "
                "AND coalesce(s.in_taxon, $species) = $species AND coalesce(o.in_taxon, $species) = $species))"
            )
        where = " WITH s, r, o WHERE " + " AND ".join(conds)
        params = {
            "subject": subject,
            "object": object,
            "types": types,
            "object_category": object_category,
            "knowledge_level": knowledge_level,
            "species": species,
        }
        total = self.run(match + where + " RETURN count(r) AS n", **params)[0]["n"]
        rows = self.run(
            match + where + " RETURN s, r, type(r) AS t, o "
            "ORDER BY coalesce(r.evidence_count, 0) DESC, coalesce(r.max_pct_expressing, 0) DESC, r.id "
            "SKIP $offset LIMIT $limit",
            offset=offset,
            limit=limit,
            **params,
        )
        return [edge_dict(r["r"], r["t"], _brief(node_dict(r["s"])), _brief(node_dict(r["o"]))) for r in rows], total

    def find_edge(self, edge_id: str) -> dict | None:
        # Relationship property indexes (post_import.cypher: rel_id_<type>) are only used with a
        # static type, so this query names each type. The names come from the database's own
        # relationship types filtered through the vendored Biolink predicate list and a strict
        # identifier pattern (_rel_type), never from the request; the edge id is a parameter.
        types = sorted(self.predicate_types())
        if not types:
            return None
        union = " UNION ALL ".join(
            f"MATCH (s:NamedThing)-[r:{_rel_type(t)}]->(o:NamedThing) WHERE r.id = $eid RETURN s, r, type(r) AS t, o"
            for t in types
        )
        rows = self.run(f"CALL () {{ {union} }} RETURN s, r, t, o LIMIT 1", eid=edge_id)
        if not rows:
            return None
        r = rows[0]
        edge = edge_dict(r["r"], r["t"], _brief(node_dict(r["s"])), _brief(node_dict(r["o"])))
        edge["publications"] = (node_dict(r["r"]).get("publications") or [])[:100]
        return edge

    def study_results(self, edge_id: str, limit: int, offset: int) -> tuple[list[dict], int]:
        total = self.run("MATCH (sr:StudyResult {edge_id: $eid}) RETURN count(sr) AS n", eid=edge_id)[0]["n"]
        rows = self.run(
            "MATCH (sr:StudyResult {edge_id: $eid}) "
            "OPTIONAL MATCH (sr)-[:supporting_data_set]->(d:Dataset) "
            "RETURN sr, d.id AS dataset, d.name AS dataset_name "
            "ORDER BY coalesce(sr.pct_expressing, -1.0) DESC, sr.id SKIP $offset LIMIT $limit",
            eid=edge_id,
            offset=offset,
            limit=limit,
        )
        out = []
        for r in rows:
            sr = node_dict(r["sr"])
            sr.pop("category", None)
            if r["dataset"] and not sr.get("dataset_id"):
                sr["dataset_id"] = r["dataset"]
            if r["dataset_name"]:
                sr["dataset_name"] = r["dataset_name"]
            if isinstance(sr.get("supporting_text"), str):
                sr["supporting_text"] = sr["supporting_text"][:300]
            out.append(sr)
        return out, total

    def tissue_descendants(self, curie: str, max_hops: int = 6, limit: int = 2000) -> tuple[list[str], list[str]]:
        """UBERON terms that are part_of / subclass_of ``curie`` (0..max_hops, the term itself included),
        and the relationship types used. Only types present in the release are followed."""
        types = [t for t in ("part_of", "subclass_of") if t in self.rel_types()]
        if not types:
            return [curie], []
        hops = max(0, min(int(max_hops), 10))
        # Fixed text: the types are constants from this module, never request values.
        rows = self.run(
            f"MATCH (d:NamedThing)-[:{'|'.join(types)}*0..{hops}]->(:NamedThing {{id: $id}}) "
            "WHERE d.id STARTS WITH 'UBERON:' RETURN DISTINCT d.id AS id ORDER BY id LIMIT $limit",
            id=curie,
            limit=limit,
        )
        terms = [r["id"] for r in rows]
        if curie not in terms:
            terms.insert(0, curie)
        return terms, types

    # -- gene sets -----------------------------------------------------------------------------
    def expressed_candidates(
        self, *, entity: str, dataset: str | None, min_pct: float | None, descendants: bool, max_rows: int
    ) -> tuple[list[dict], bool]:
        """Every (gene, expressed_in edge, StudyResult) supporting the entity (or its subclass_of descendants),
        for ranking outside the graph. Returns rows and whether ``max_rows`` cut the list."""
        hop = "-[:subclass_of*0..3]->" if descendants and SUBCLASS_OF in self.rel_types() else ""
        target = (
            f"MATCH (t:NamedThing){hop}(e:NamedThing {{id: $entity}})" if hop else "MATCH (t:NamedThing {id: $entity})"
        )
        conds = ["sr.edge_id = r.id"]
        if dataset:
            conds.append("sr.dataset_id = $dataset")
        if min_pct is not None:
            conds.append("sr.pct_expressing >= $min_pct")
        rows = self.run(
            target + " WITH DISTINCT t MATCH (g:Gene)-[r:expressed_in]->(t) WHERE " + NOT_REJECTED + " "
            "MATCH (sr:StudyResult {edge_id: r.id}) WHERE " + " AND ".join(conds) + " "
            "RETURN g.id AS gene_id, g.symbol AS symbol, g.name AS name, r.id AS edge_id, r.predicate AS predicate, "
            "t.id AS target, t.name AS target_name, r.knowledge_level AS knowledge_level, "
            "r.primary_knowledge_source AS primary_knowledge_source, r.max_pct_expressing AS max_pct_expressing, "
            "r.max_mean_log1p_cp10k AS max_mean_log1p_cp10k, r.n_datasets AS n_datasets, "
            "r.evidence_count AS evidence_count, sr.dataset_id AS dataset_id, sr.level AS level, "
            "sr.group_label AS group_label, sr.pct_expressing AS pct_expressing, "
            "sr.pseudobulk_cp10k AS pseudobulk_cp10k "
            "ORDER BY sr.pct_expressing DESC LIMIT $cap",
            entity=entity,
            dataset=dataset,
            min_pct=min_pct,
            cap=max_rows + 1,
        )
        return [dict(r) for r in rows[:max_rows]], len(rows) > max_rows

    def gene_set(
        self,
        *,
        entity: str,
        kind: str,
        dataset: str | None,
        min_pct: float | None,
        descendants: bool,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int, list[str]]:
        present = self.rel_types()
        notes: list[str] = []
        hop = ""
        if descendants and SUBCLASS_OF in present:
            hop = "-[:subclass_of*0..3]->"
        if kind == "expressed":
            if EXPRESSED_IN not in present:
                return [], 0, ["release has no expressed_in edges"]
            types = [EXPRESSED_IN]
        else:
            types = [t for t in DISEASE_GENE_PREDICATES if t in present]
            if not types:
                return [], 0, ["release has no gene-to-condition edges"]
            notes.append("predicates: " + ", ".join(f"biolink:{t}" for t in types))
        target = (
            f"MATCH (t:NamedThing){hop}(e:NamedThing {{id: $entity}})"
            if hop
            else ("MATCH (t:NamedThing {id: $entity})")
        )
        base = target + " MATCH (g:Gene)-[r:$any($types)]->(t) WHERE " + NOT_REJECTED
        params: dict[str, Any] = {"entity": entity, "types": types}
        if kind == "expressed" and dataset:
            base += (
                " MATCH (sr:StudyResult {edge_id: r.id}) WHERE sr.dataset_id = $dataset"
                + (" AND sr.pct_expressing >= $min_pct" if min_pct is not None else "")
                + " WITH g, r, t, max(sr.pct_expressing) AS s"
            )
            params.update(dataset=dataset, min_pct=min_pct)
        elif kind == "expressed":
            if min_pct is not None:
                base += " AND r.max_pct_expressing >= $min_pct"
                params["min_pct"] = min_pct
            base += " WITH g, r, t, r.max_pct_expressing AS s"
        else:
            base += " WITH g, r, t, 1.0 AS s"
        agg = (
            base + " WITH DISTINCT g, r, t, s"
            " WITH g, collect({edge_id: r.id, predicate: r.predicate, target: t.id, target_name: t.name,"
            " knowledge_level: r.knowledge_level, primary_knowledge_source: r.primary_knowledge_source,"
            " max_pct_expressing: r.max_pct_expressing, max_mean_log1p_cp10k: r.max_mean_log1p_cp10k,"
            " n_datasets: r.n_datasets, evidence_count: r.evidence_count, s: s}) AS ev,"
            " max(s) AS best, count(r) AS n_edges"
        )
        total = self.run(agg + " RETURN count(g) AS n", **params)[0]["n"]
        rows = self.run(
            agg + " RETURN g.id AS gene_id, g.symbol AS symbol, g.name AS name, best, n_edges, ev "
            "ORDER BY best DESC, n_edges DESC, symbol SKIP $offset LIMIT $limit",
            offset=offset,
            limit=limit,
            **params,
        )
        out = []
        for r in rows:
            ev = sorted(r["ev"], key=lambda e: -(e.get("s") or 0))[:5]
            for e in ev:
                e.pop("s", None)
                for k in [k for k, v in e.items() if v is None]:
                    e.pop(k)
            score = r["best"] if kind == "expressed" else float(r["n_edges"])
            out.append(
                {
                    "gene_id": r["gene_id"],
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "score": round(float(score), 6) if score is not None else None,
                    "n_edges": r["n_edges"],
                    "evidence": ev,
                }
            )
        return out, total, notes
