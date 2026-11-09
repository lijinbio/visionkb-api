"""Request models (validated) and response models (OpenAPI documentation only)."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .vocab import agent_types, is_curie, knowledge_levels, normalize_predicate, predicate_info, qualifier_names

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,199}$")
INFORES_RE = re.compile(r"^infores:[a-z0-9][a-z0-9.\-]{0,100}$")
MAX_EVIDENCE_ITEM_CHARS = 4000


# -- responses ---------------------------------------------------------------------------------
class Page(BaseModel):
    limit: int
    offset: int
    total: int | None = None


class ErrorInfo(BaseModel):
    code: str
    message: str


class Envelope[T](BaseModel):
    """Every response: ``data`` is the payload; ``kg_release`` names the release it came from."""

    status: Literal["ok", "error"]
    kg_release: str
    data: T | None = None
    page: Page | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class NodeRef(_Open):
    id: str
    name: str | None = None
    category: list[str] = Field(default_factory=list)


class EntityHit(_Open):
    id: str
    name: str | None = None
    category: list[str]
    symbol: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    match: Literal["exact_id", "exact_symbol", "synonym", "fulltext"]
    score: float


class EntityDetail(_Open):
    node: dict[str, Any]
    degree_by_predicate: dict[str, dict[str, int]]


class Relationship(_Open):
    edge_id: str
    subject: NodeRef
    predicate: str
    object: NodeRef
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    knowledge_level: str | None = None
    agent_type: str | None = None
    primary_knowledge_source: str | None = None
    publications: list[str] = Field(default_factory=list, description="at most 5; see n_publications")
    n_publications: int = 0
    evidence_count: int | None = None
    status: str | None = None
    summary: dict[str, Any] | None = None


class Evidence(_Open):
    edge: Relationship
    study_results: list[dict[str, Any]]
    publications: list[str]


class GeneSetItem(_Open):
    gene_id: str
    symbol: str | None = None
    score: float | None = None
    evidence: list[dict[str, Any]]


class ExpressionRow(_Open):
    dataset_id: str
    atlas_version: str
    dataset_version: str
    level: str
    group_label: str
    cell_type_id: str | None = None
    region: str | None = None
    gene_id: str
    gene_symbol: str | None = None
    n_cells: int
    n_donors: int | None = None
    n_expressing: int | None = None
    pct_expressing: float | None = None
    mean_log1p_cp10k: float | None = None
    pseudobulk_cp10k: float | None = None
    n_donors_expressing: int | None = None


class DatasetHit(_Open):
    id: str
    version: str | None = None
    title: str | None = None
    bind: dict[str, Any]
    score: float
    matched: list[dict[str, Any]]


class ProposalCreated(BaseModel):
    proposal_id: str
    status: Literal["proposed"]
    created: bool = Field(description="false if the same run_id already proposed this edge")


class Health(_Open):
    neo4j: dict[str, Any]
    summaries: dict[str, Any]
    postgres: dict[str, Any]
    release: str
    version: str


# -- requests ----------------------------------------------------------------------------------
QualifierValue = str | list[str]


class ProposedEdge(BaseModel):
    """KGX-style edge. Qualifiers may be given flat (KGX) or under ``qualifiers``."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(examples=["HGNC:16665"])
    predicate: str = Field(examples=["biolink:expressed_in"])
    object: str = Field(examples=["CL:0000115"])
    knowledge_level: str = Field(examples=["observation"])
    agent_type: str = Field(examples=["data_analysis_pipeline"])
    primary_knowledge_source: str = "infores:visionkb-aiscientist"
    publications: list[str] = Field(default_factory=list, max_length=50)
    qualifiers: dict[str, QualifierValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _flat_qualifiers(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            quals = dict(data.get("qualifiers") or {})
            for k in [k for k in data if k in qualifier_names()]:
                quals[k] = data.pop(k)
            data["qualifiers"] = quals
        return data

    @field_validator("subject", "object")
    @classmethod
    def _curie(cls, v: str) -> str:
        if not is_curie(v):
            raise ValueError(f"{v!r} is not a CURIE with a known prefix")
        return v

    @field_validator("predicate")
    @classmethod
    def _predicate(cls, v: str) -> str:
        p = normalize_predicate(v)
        info = predicate_info(p)
        if info is None:
            raise ValueError(f"{v!r} is not a Biolink 4.4.6 predicate")
        if info["mixin"] or info["abstract"]:
            raise ValueError(f"{p} is a mixin or abstract predicate; use a concrete one")
        return p

    @field_validator("knowledge_level")
    @classmethod
    def _kl(cls, v: str) -> str:
        if v not in knowledge_levels():
            raise ValueError(f"knowledge_level must be one of {knowledge_levels()}")
        return v

    @field_validator("agent_type")
    @classmethod
    def _at(cls, v: str) -> str:
        if v not in agent_types():
            raise ValueError(f"agent_type must be one of {agent_types()}")
        return v

    @field_validator("primary_knowledge_source")
    @classmethod
    def _infores(cls, v: str) -> str:
        if not INFORES_RE.match(v):
            raise ValueError("primary_knowledge_source must be an infores: CURIE")
        return v

    @field_validator("publications")
    @classmethod
    def _pubs(cls, v: list[str]) -> list[str]:
        for p in v:
            if not (p.startswith(("PMID:", "doi:", "DOI:")) and is_curie(p)):
                raise ValueError(f"publication {p!r} must be PMID:<n> or doi:<10.x/...>")
        return v

    @field_validator("qualifiers")
    @classmethod
    def _quals(cls, v: dict[str, QualifierValue]) -> dict[str, QualifierValue]:
        allowed = qualifier_names()
        for k, val in v.items():
            if k not in allowed:
                raise ValueError(f"{k!r} is not a Biolink qualifier")
            vals = val if isinstance(val, list) else [val]
            if len(vals) > 20 or any(len(x) > 200 for x in vals):
                raise ValueError(f"qualifier {k!r}: at most 20 values of at most 200 characters")
        return v

    def kgx(self) -> dict:
        """Flat KGX edge as stored."""
        out = {
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "knowledge_level": self.knowledge_level,
            "agent_type": self.agent_type,
            "primary_knowledge_source": self.primary_knowledge_source,
            "publications": self.publications,
        }
        out.update(dict(sorted(self.qualifiers.items())))
        return out


class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge: ProposedEdge
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    run_id: str = Field(examples=["aiscientist-run-0001"])
    visibility: Literal["owner", "lab", "public"] = "owner"

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, v: str) -> str:
        if not RUN_ID_RE.match(v):
            raise ValueError("run_id: 1-200 characters of [A-Za-z0-9._:-]")
        return v

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, item in enumerate(v):
            if len(json.dumps(item, default=str)) > MAX_EVIDENCE_ITEM_CHARS:
                raise ValueError(f"evidence[{i}] is longer than {MAX_EVIDENCE_ITEM_CHARS} characters")
        return v
