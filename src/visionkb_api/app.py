"""FastAPI application: read-only endpoints over a KG release plus the proposals queue (contracts §8)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Path, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .auth import Principal, TokenStore
from .config import DEFAULT_LIMIT, MAX_OFFSET, ConfigError, Settings, read_secret
from .datasets import find_datasets
from .envelope import ApiError, envelope, fit
from .models import (
    DatasetHit,
    EntityDetail,
    EntityHit,
    Envelope,
    Evidence,
    ExpressionRow,
    GeneSetItem,
    Health,
    ProposalCreated,
    ProposalIn,
    Relationship,
)
from .proposals import STATUSES, ProposalStore
from .ranking import SCORE_DEFINITION, rank_by_specificity
from .release import Release, load_release
from .summaries import LEVELS, MAX_GENES, SORTS, Summaries
from .vocab import (
    EDGE_ID_RE,
    PREFIX_PATTERNS,
    is_category,
    is_curie,
    knowledge_levels,
    normalize_category,
    normalize_predicate,
    predicate_info,
)

log = logging.getLogger("visionkb_api")
MAX_BODY_BYTES = 64 * 1024
GENE_SET_PREFIXES = ("CL:", "visionkb:cs-", "MONDO:", "HP:")
MAX_GENESET_CANDIDATES = 100_000
Limit = Annotated[int, Query(ge=1, le=10_000, description="rows to return (default 50, capped at 200)")]
Offset = Annotated[int, Query(ge=0, le=MAX_OFFSET)]
bearer = HTTPBearer(auto_error=False, description="Token from VISIONKB_API_TOKENS_FILE")


class State:
    """Components built at startup. Release problems are fatal; database problems are reported."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.release: Release | None = None
        self.summaries: Summaries | None = None
        self.summaries_error: str | None = None
        self.graph: Any = None
        self.graph_error: str | None = None
        self.proposals: ProposalStore | None = None
        self.proposals_error: str | None = None
        self.tokens = TokenStore(settings.tokens_file)

    @property
    def kg_release(self) -> str:
        return self.release.kg_release if self.release else "unknown"

    def startup(self) -> None:
        s = self.settings
        self.release = load_release(s.release_dir, s.kg_release, verify_checksums=s.verify_checksums)
        try:
            self.summaries = Summaries(self.release.summaries_path)
        except Exception as exc:  # reported by /v1/health; atlas endpoints return 503
            self.summaries_error = str(exc)
            log.error("summaries unavailable: %s", exc)
        if s.neo4j_uri:
            try:
                from .graph import Graph

                password = read_secret(s.neo4j_password_file) if s.neo4j_password_file else ""
                self.graph = Graph(s.neo4j_uri, s.neo4j_user, password, s.neo4j_database, s.neo4j_timeout_s)
            except ConfigError as exc:
                self.graph_error = str(exc)
        else:
            self.graph_error = "NEO4J_URI is not set"
        if s.pg_dsn_file:
            try:
                self.proposals = ProposalStore(read_secret(s.pg_dsn_file))
                self.proposals.migrate()
            except ConfigError as exc:
                self.proposals, self.proposals_error = None, str(exc)
            except ApiError as exc:  # database down at startup: retried on first use
                self.proposals_error = exc.message
                log.warning("proposals store: %s (migration retried on first use)", exc.message)
        else:
            self.proposals_error = "VISIONKB_PG_DSN_FILE is not set"
        log.info("serving %s from %s", self.kg_release, self.release.path)

    def shutdown(self) -> None:
        if self.graph is not None:
            self.graph.close()

    # -- component access ---------------------------------------------------------------------
    def need_graph(self):
        if self.graph is None:
            raise ApiError(503, "neo4j_unavailable", f"graph not configured: {self.graph_error}")
        return self.graph

    def need_summaries(self) -> Summaries:
        if self.summaries is None:
            raise ApiError(503, "summaries_unavailable", f"expression summaries not loaded: {self.summaries_error}")
        return self.summaries

    def need_proposals(self) -> ProposalStore:
        if self.proposals is None:
            raise ApiError(503, "postgres_unavailable", f"proposals store not configured: {self.proposals_error}")
        return self.proposals


def _state(request: Request) -> State:
    return request.app.state.vkb


def scope(name: str):
    def dep(
        request: Request, cred: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)] = None
    ) -> Principal:
        return _state(request).tokens.authenticate(request.headers.get("authorization"), name)

    return dep


class Ctx:
    """Per-request helper: limits, warnings and the enveloped response."""

    def __init__(self, request: Request) -> None:
        self.st = _state(request)
        self.warnings: list[str] = []

    def limit(self, limit: int) -> int:
        cap = self.st.settings.max_rows
        if limit > cap:
            self.warnings.append(f"limit {limit} capped at {cap}")
            return cap
        return limit

    def respond(self, data: Any, *, page: dict | None = None, status_code: int = 200) -> JSONResponse:
        env = envelope(self.st.kg_release, data, page=page, warnings=self.warnings)
        return JSONResponse(fit(env, self.st.settings.max_chars), status_code=status_code)


def _curie(value: str, what: str) -> str:
    v = value.strip()
    if not is_curie(v):
        raise ApiError(
            400, "invalid_curie", f"{what} {value!r} is not a CURIE with a known prefix ({', '.join(PREFIX_PATTERNS)})"
        )
    return v


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return list(dict.fromkeys(out))


def _err(st: State | None, status: int, code: str, message: str) -> JSONResponse:
    rel = st.kg_release if st else "unknown"
    return JSONResponse(envelope(rel, None, error={"code": code, "message": message}), status_code=status)


def tag_known_issues(rows: list[dict], release: Release, warnings: list[str], what: str) -> None:
    """Add ``known_issues: [ids]`` to rows whose (dataset_id, group_label) a manifest known issue flags,
    and one warning per issue id naming the groups (at most 10)."""
    if not release.known_issue_groups:
        return
    hits: dict[str, dict[str, str | None]] = {}
    for row in rows:
        tags = release.known_issues_for(row.get("dataset_id"), row.get("group_label"), row.get("level"))
        if tags:
            row["known_issues"] = sorted({t["id"] for t in tags})
            for t in tags:
                hits.setdefault(t["id"], {})[f"{row.get('dataset_id')}|{row.get('group_label')}"] = t.get("program_of")
    titles = {
        ki.get("id"): ki.get("title") for ki in release.manifest.get("known_issues") or [] if isinstance(ki, dict)
    }
    for ki_id, groups in sorted(hits.items()):
        n = sum(1 for r in rows if ki_id in r.get("known_issues", []))
        listed = ", ".join(f"{g} ({p} program)" if p else g for g, p in list(groups.items())[:10])
        more = f" and {len(groups) - 10} more" if len(groups) > 10 else ""
        title = f" ({titles[ki_id]})" if titles.get(ki_id) else ""
        warnings.append(
            f"{ki_id}{title}: {n} of {len(rows)} returned {what} are in flagged groups: {listed}{more}; "
            "see /v1/release known_issues"
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = State(settings or Settings.from_env())
        st.startup()
        app.state.vkb = st
        try:
            yield
        finally:
            st.shutdown()

    app = FastAPI(
        title="VisionKB API",
        version=__version__,
        summary="Read-only access to a VisionKB knowledge-graph release, plus a curation queue for proposals.",
        description=(
            "Every response is an envelope `{status, kg_release, data, page, warnings, error}`. "
            "Lists default to 50 rows (max 200) and responses aim at 24,000 characters at most; "
            "trimmed lists are reported in `warnings`."
        ),
        lifespan=lifespan,
        docs_url="/v1/docs",
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            size = request.headers.get("content-length")
            if size is None or not size.isdigit():
                return _err(getattr(request.app.state, "vkb", None), 411, "length_required", "Content-Length required")
            if int(size) > MAX_BODY_BYTES:
                return _err(
                    getattr(request.app.state, "vkb", None), 413, "too_large", f"body over {MAX_BODY_BYTES} bytes"
                )
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _err(getattr(request.app.state, "vkb", None), exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        parts = []
        for e in exc.errors()[:5]:
            loc = ".".join(str(x) for x in e.get("loc", ()) if x != "body")
            parts.append(f"{loc}: {e.get('msg')}")
        return _err(getattr(request.app.state, "vkb", None), 422, "invalid_request", "; ".join(parts))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _err(getattr(request.app.state, "vkb", None), exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return _err(getattr(request.app.state, "vkb", None), 500, "internal_error", "internal error")

    read = Depends(scope("read"))

    # -- service -------------------------------------------------------------------------------
    def components(st: State) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Status of neo4j, summaries and postgres (live pings), plus warnings for each one not ok."""
        notes: list[str] = []
        neo: dict[str, Any] = {"status": "not_configured", "detail": st.graph_error}
        if st.graph is not None:
            try:
                info = st.graph.ping()
                neo = {"status": "ok", **info}
                if info.get("kg_release") and info["kg_release"] != st.kg_release:
                    neo["status"] = "mismatch"
                    notes.append(f"Neo4j holds {info['kg_release']}, the API serves {st.kg_release}")
            except ApiError as exc:
                neo = {"status": "error", "detail": exc.message}
        summ = (
            {"status": "ok", "n_rows": st.summaries.n_rows}
            if st.summaries
            else {"status": "error", "detail": st.summaries_error}
        )
        pg: dict[str, Any] = {"status": "not_configured", "detail": st.proposals_error}
        if st.proposals is not None:
            try:
                pg = {"status": "ok", **st.proposals.ping()}
            except ApiError as exc:
                pg = {"status": "error", "detail": exc.message}
        comps = {"neo4j": neo, "summaries": summ, "postgres": pg}
        notes += [f"{name}: {c['status']}" for name, c in comps.items() if c["status"] != "ok"]
        return comps, notes

    @app.get("/v1/health", tags=["service"], response_model=Envelope[Health])
    def health(request: Request):
        """Component status for people and monitoring. No authentication. HTTP 200 while the process serves
        the release; components that are not ok are listed in `warnings`. Use `/v1/ready` for readiness probes."""
        ctx = Ctx(request)
        comps, notes = components(ctx.st)
        ctx.warnings += notes
        return ctx.respond({**comps, "release": ctx.st.kg_release, "version": __version__})

    @app.get(
        "/v1/ready",
        tags=["service"],
        response_model=Envelope[Health],
        responses={503: {"model": Envelope[Health], "description": "a component is not ok; error.message lists them"}},
    )
    def ready(request: Request):
        """Readiness. No authentication. HTTP 200 only when the release is loaded and neo4j, summaries and
        postgres are all ok; otherwise HTTP 503, `status: error`, code `not_ready`, and the failing components
        in `error.message` and `warnings`. `data` carries the same component status as `/v1/health`."""
        ctx = Ctx(request)
        st = ctx.st
        comps, notes = components(st)
        ctx.warnings += notes
        data = {**comps, "release": st.kg_release, "version": __version__}
        failing = [name for name, c in comps.items() if c["status"] != "ok"]
        if st.release is None:
            failing.insert(0, "release")
        if not failing:
            return ctx.respond(data)
        env = envelope(
            st.kg_release,
            data,
            warnings=ctx.warnings,
            error={"code": "not_ready", "message": "not ready: " + ", ".join(failing)},
        )
        return JSONResponse(fit(env, st.settings.max_chars), status_code=503)

    @app.get("/v1/release", tags=["service"], dependencies=[read])
    def release(request: Request):
        """Manifest subset (§6): release id, Biolink version, code versions, sources, atlases, counts, validation."""
        ctx = Ctx(request)
        data = ctx.st.release.manifest_subset()
        if ctx.st.graph is not None:
            try:
                data["predicates_in_graph"] = sorted(f"biolink:{t}" for t in ctx.st.graph.predicate_types())
            except ApiError as exc:
                ctx.warnings.append(f"graph: {exc.message}")
        return ctx.respond(data)

    # -- entities ------------------------------------------------------------------------------
    @app.get("/v1/entities/search", tags=["graph"], dependencies=[read], response_model=Envelope[list[EntityHit]])
    def search_entities(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=200, description="CURIE, symbol, synonym or free text")],
        category: Annotated[str | None, Query(description="Biolink category, e.g. biolink:Gene")] = None,
        limit: Limit = DEFAULT_LIMIT,
    ):
        """Exact CURIE, then exact symbol (case-insensitive), then synonym, then the `entity_search` full-text index."""
        ctx = Ctx(request)
        cat = None
        if category:
            cat = normalize_category(category)
            if not is_category(cat):
                raise ApiError(400, "invalid_category", f"{category!r} is not a Biolink category")
        n = ctx.limit(limit)
        hits = ctx.st.need_graph().search(q, cat, n)
        return ctx.respond(hits, page={"limit": n, "offset": 0, "total": None})

    @app.get("/v1/entities/{curie}", tags=["graph"], dependencies=[read], response_model=Envelope[EntityDetail])
    def get_entity(request: Request, curie: Annotated[str, Path(description="CURIE, e.g. HGNC:10012")]):
        """One node with its degree by predicate and direction."""
        ctx = Ctx(request)
        node = ctx.st.need_graph().get_node(_curie(curie, "curie"))
        if node is None:
            raise ApiError(404, "not_found", f"{curie} is not in {ctx.st.kg_release}")
        return ctx.respond(node)

    # -- relationships and evidence -------------------------------------------------------------
    @app.get("/v1/relationships", tags=["graph"], dependencies=[read], response_model=Envelope[list[Relationship]])
    def get_relationships(
        request: Request,
        subject: str | None = None,
        object: str | None = None,
        predicate: Annotated[str | None, Query(description="Biolink predicate present in the release")] = None,
        object_category: str | None = None,
        knowledge_level: str | None = None,
        species: Annotated[str | None, Query(description="NCBITaxon CURIE")] = None,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ):
        """Edges from `subject` and/or to `object` (at least one is required), ranked by evidence."""
        ctx = Ctx(request)
        if not subject and not object:
            raise ApiError(400, "missing_anchor", "give subject or object (or both)")
        s = _curie(subject, "subject") if subject else None
        o = _curie(object, "object") if object else None
        graph = ctx.st.need_graph()
        present = graph.predicate_types()
        if predicate:
            p = normalize_predicate(predicate)
            info = predicate_info(p)
            if info is None:
                raise ApiError(400, "unknown_predicate", f"{predicate!r} is not a Biolink 4.4.6 predicate")
            if p.removeprefix("biolink:") not in present:
                raise ApiError(
                    400,
                    "predicate_not_in_release",
                    f"{p} is not in {ctx.st.kg_release}; present: {', '.join(sorted(present))}",
                )
            if info["deprecated"]:
                ctx.warnings.append(f"{p} is deprecated in Biolink 4.4.6")
            types = [p.removeprefix("biolink:")]
        else:
            types = sorted(present)
        oc = None
        if object_category:
            oc = normalize_category(object_category)
            if not is_category(oc):
                raise ApiError(400, "invalid_category", f"{object_category!r} is not a Biolink category")
        if knowledge_level and knowledge_level not in knowledge_levels():
            raise ApiError(400, "invalid_knowledge_level", f"knowledge_level must be one of {knowledge_levels()}")
        sp = None
        if species:
            sp = _curie(species, "species")
            if not sp.startswith("NCBITaxon:"):
                raise ApiError(400, "invalid_curie", "species must be an NCBITaxon CURIE")
        n = ctx.limit(limit)
        rows, total = graph.relationships(
            subject=s,
            object=o,
            types=types,
            object_category=oc,
            knowledge_level=knowledge_level,
            species=sp,
            limit=n,
            offset=offset,
        )
        return ctx.respond(rows, page={"limit": n, "offset": offset, "total": total})

    @app.get("/v1/evidence/{edge_id}", tags=["graph"], dependencies=[read], response_model=Envelope[Evidence])
    def get_evidence(
        request: Request,
        edge_id: Annotated[str, Path(description="visionkb:e-<sha1>")],
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ):
        """The edge, its StudyResult nodes (paged) and its publications."""
        ctx = Ctx(request)
        if not EDGE_ID_RE.match(edge_id):
            raise ApiError(400, "invalid_edge_id", "edge_id must look like visionkb:e-<40 hex>")
        graph = ctx.st.need_graph()
        edge = graph.find_edge(edge_id)
        if edge is None:
            raise ApiError(404, "not_found", f"{edge_id} is not in {ctx.st.kg_release}")
        n = ctx.limit(limit)
        srs, total = graph.study_results(edge_id, n, offset)
        pubs = list(edge.get("publications") or [])
        for sr in srs:
            pmid = sr.get("pmid")
            if pmid:
                pmid = pmid if str(pmid).startswith("PMID:") else f"PMID:{pmid}"
                if pmid not in pubs:
                    pubs.append(pmid)
        edge["publications"] = edge["publications"][:5]
        tag_known_issues(srs, ctx.st.release, ctx.warnings, "study results")
        data = {"edge": edge, "study_results": srs, "publications": pubs}
        return ctx.respond(data, page={"limit": n, "offset": offset, "total": total})

    @app.get("/v1/gene-sets", tags=["graph"], dependencies=[read], response_model=Envelope[list[GeneSetItem]])
    def get_gene_set(
        request: Request,
        entity: Annotated[str, Query(description="CL, CellState (visionkb:cs-...), MONDO or HP CURIE")],
        kind: Annotated[
            Literal["expressed", "disease_genes"] | None,
            Query(description="default: expressed for CL/CellState, disease_genes for MONDO/HP"),
        ] = None,
        dataset: Annotated[str | None, Query(description="restrict expression evidence to one dataset")] = None,
        min_pct: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
        descendants: Annotated[bool, Query(description="include subclass_of descendants (up to 3 hops)")] = True,
        sort: Annotated[
            Literal["specificity", "pct"],
            Query(description="kind=expressed: specificity (default) or pct (max pct_expressing, v0.1.2 behaviour)"),
        ] = "specificity",
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ):
        """Genes expressed in a cell type/state, or genes associated with a disease/phenotype."""
        ctx = Ctx(request)
        e = _curie(entity, "entity")
        if not e.startswith(GENE_SET_PREFIXES):
            raise ApiError(400, "invalid_entity", "entity must be a CL, CellState, MONDO or HP CURIE")
        k = kind or ("expressed" if e.startswith(("CL:", "visionkb:cs-")) else "disease_genes")
        if dataset:
            dataset = _curie(dataset, "dataset")
            if k != "expressed":
                ctx.warnings.append("dataset applies to kind=expressed only; ignored")
        rel = ctx.st.release
        if k == "expressed" and min_pct is not None and min_pct < rel.min_pct_for_edges:
            ctx.warnings.append(
                f"min_pct {min_pct} is below the edge rule's pct_expressing >= {rel.min_pct_for_edges}: "
                "lower values cannot add genes; use /v1/atlas/expression for all genes x groups"
            )
        graph = ctx.st.need_graph()
        if not graph.exists([e]).get(e):
            raise ApiError(404, "not_found", f"{e} is not in {ctx.st.kg_release}")
        n = ctx.limit(limit)
        if k == "expressed" and sort == "specificity":
            summ = ctx.st.need_summaries()
            cands, capped = graph.expressed_candidates(
                entity=e, dataset=dataset, min_pct=min_pct, descendants=descendants, max_rows=MAX_GENESET_CANDIDATES
            )
            if capped:
                ctx.warnings.append(
                    f"ranking uses the first {MAX_GENESET_CANDIDATES} supporting StudyResults (by pct_expressing)"
                )
            top2 = summ.other_group_max(sorted({c["gene_id"] for c in cands}))
            rows, total = rank_by_specificity(cands, top2, n, offset)
            ctx.warnings.append(SCORE_DEFINITION)
            ctx.warnings.append(f"expressed_in edges exist only where {rel.edge_rule_text()}")
            return ctx.respond(rows, page={"limit": n, "offset": offset, "total": total})
        rows, total, notes = graph.gene_set(
            entity=e,
            kind=k,
            dataset=dataset if k == "expressed" else None,
            min_pct=min_pct if k == "expressed" else None,
            descendants=descendants,
            limit=n,
            offset=offset,
        )
        ctx.warnings += notes
        if k == "expressed":
            ctx.warnings.append("score = max pct_expressing over supporting edges (sort=pct)")
            ctx.warnings.append(f"expressed_in edges exist only where {rel.edge_rule_text()}")
        else:
            ctx.warnings.append("score = number of supporting gene-condition edges")
        return ctx.respond(rows, page={"limit": n, "offset": offset, "total": total})

    # -- atlas and datasets --------------------------------------------------------------------
    @app.get("/v1/atlas/expression", tags=["atlas"], dependencies=[read], response_model=Envelope[list[ExpressionRow]])
    def atlas_expression(
        request: Request,
        genes: Annotated[
            list[str], Query(description="gene CURIEs or symbols, comma-separated or repeated; at most 20")
        ],
        dataset: str | None = None,
        level: Annotated[Literal[LEVELS], Query()] = "majorclass",  # type: ignore[valid-type]
        cell_type: Annotated[str | None, Query(description="cell_type_id CURIE or group label")] = None,
        sort: Annotated[Literal[SORTS], Query()] = "pseudobulk_cp10k",  # type: ignore[valid-type]
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ):
        """Rows of `summaries/expr_summary.parquet` (all genes x groups, no threshold), read with DuckDB."""
        ctx = Ctx(request)
        names = _split(genes)
        if not names:
            raise ApiError(400, "missing_genes", "give at least one gene")
        if len(names) > MAX_GENES:
            raise ApiError(400, "too_many_genes", f"at most {MAX_GENES} genes per request")
        if any(len(g) > 100 for g in names):
            raise ApiError(400, "invalid_gene", "gene names are at most 100 characters")
        if dataset:
            dataset = _curie(dataset, "dataset")
        summ = ctx.st.need_summaries()
        found, missing = summ.resolve_genes(names)
        for g in missing:
            ctx.warnings.append(f"{g}: not found in the expression summaries")
        for g, pairs in found.items():
            if len(pairs) > 1:
                ctx.warnings.append(f"{g}: matches {len(pairs)} genes ({', '.join(p[0] for p in pairs)}); all returned")
        ids = sorted({p[0] for pairs in found.values() for p in pairs})
        n = ctx.limit(limit)
        if not ids:
            return ctx.respond([], page={"limit": n, "offset": offset, "total": 0})
        rows, total = summ.expression(
            ids, dataset=dataset, level=level, cell_type=cell_type, sort=sort, limit=n, offset=offset
        )
        tag_known_issues(rows, ctx.st.release, ctx.warnings, "rows")
        return ctx.respond(rows, page={"limit": n, "offset": offset, "total": total})

    @app.get("/v1/datasets", tags=["atlas"], dependencies=[read], response_model=Envelope[list[DatasetHit]])
    def get_datasets(
        request: Request,
        entities: Annotated[
            list[str] | None, Query(description="CURIEs: cell types/states, genes, tissue, disease, assay")
        ] = None,
        species: str | None = None,
        tissue: str | None = None,
        assay: str | None = None,
        disease: str | None = None,
        expand: Annotated[
            bool, Query(description="tissue: also match UBERON terms that are part_of/subclass_of it (release graph)")
        ] = True,
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ):
        """Registry records (§4) with `bind` for AiScientist. Paths in records are metadata; the API never opens them."""
        ctx = Ctx(request)
        ents = [_curie(e, "entity") for e in _split(entities)]
        if len(ents) > 20:
            raise ApiError(400, "too_many_entities", "at most 20 entities")
        filters = {}
        for name, val in (("species", species), ("tissue", tissue), ("assay", assay), ("disease", disease)):
            filters[name] = _curie(val, name) if val else None
        st = ctx.st
        graph = st.graph
        tissue_terms = None
        if filters["tissue"] and expand:
            if graph is None:
                ctx.warnings.append("tissue not expanded: graph not configured")
            else:
                try:
                    terms, types = graph.tissue_descendants(filters["tissue"])
                    tissue_terms = set(terms)
                    shown = ", ".join(terms[:25]) + (f" and {len(terms) - 25} more" if len(terms) > 25 else "")
                    ctx.warnings.append(
                        f"tissue {filters['tissue']} expanded via {'/'.join(types) or 'no relation types'} "
                        f"(up to 6 hops) to {len(terms)} UBERON terms: {shown}; expand=false to match exactly"
                    )
                except ApiError as exc:
                    ctx.warnings.append(f"tissue not expanded: {exc.message}")
        rows, notes = find_datasets(
            st.release.datasets,
            entities=ents,
            tissue_terms=tissue_terms,
            expressing=(lambda g: st.summaries.datasets_expressing(g)) if st.summaries else None,
            cell_states_under=graph.cell_states_under if graph is not None else None,
            **filters,
        )
        ctx.warnings += notes
        n = ctx.limit(limit)
        return ctx.respond(rows[offset : offset + n], page={"limit": n, "offset": offset, "total": len(rows)})

    # -- proposals -----------------------------------------------------------------------------
    @app.post("/v1/proposals", tags=["proposals"], status_code=201, response_model=Envelope[ProposalCreated])
    def post_proposal(request: Request, body: ProposalIn, who: Annotated[Principal, Depends(scope("proposals:write"))]):
        """Queue a KGX-style edge for curation with status `proposed`. Never written to Neo4j."""
        ctx = Ctx(request)
        edge = body.edge.kgx()
        info = predicate_info(edge["predicate"])
        if info and info["deprecated"]:
            ctx.warnings.append(f"{edge['predicate']} is deprecated in Biolink 4.4.6")
        if ctx.st.graph is not None:
            try:
                found = ctx.st.graph.exists([edge["subject"], edge["object"]])
                for k in ("subject", "object"):
                    if not found.get(edge[k]):
                        ctx.warnings.append(f"{k} {edge[k]} is not in {ctx.st.kg_release}")
                if edge["predicate"].removeprefix("biolink:") not in ctx.st.graph.predicate_types():
                    ctx.warnings.append(f"{edge['predicate']} is not used in {ctx.st.kg_release}")
            except ApiError as exc:
                ctx.warnings.append(f"subject/object not checked against the graph: {exc.message}")
        else:
            ctx.warnings.append("subject/object not checked against the graph (graph not configured)")
        pid, created = ctx.st.need_proposals().submit(
            edge=edge,
            evidence=body.evidence,
            run_id=body.run_id,
            visibility=body.visibility,
            submitted_by=who.token_id,
            kg_release=ctx.st.kg_release,
        )
        if not created:
            ctx.warnings.append("this run_id already proposed the same edge; returning the existing proposal")
        return ctx.respond(
            {"proposal_id": pid, "status": "proposed", "created": created}, status_code=201 if created else 200
        )

    @app.get("/v1/proposals", tags=["proposals"], dependencies=[Depends(scope("proposals:read"))])
    def list_proposals(
        request: Request,
        status: Annotated[Literal[STATUSES] | None, Query()] = None,  # type: ignore[valid-type]
        limit: Limit = DEFAULT_LIMIT,
        offset: Offset = 0,
    ):
        """Proposals in the curation queue, newest first."""
        ctx = Ctx(request)
        n = ctx.limit(limit)
        rows, total = ctx.st.need_proposals().list(status=status, limit=n, offset=offset)
        return ctx.respond(rows, page={"limit": n, "offset": offset, "total": total})

    return app
