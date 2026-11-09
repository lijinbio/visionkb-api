<div align="center">

# VisionKB API

**Read-only HTTP access to a VisionKB knowledge-graph release, plus a curation queue.**

[![CI](https://github.com/lijinbio/visionkb-api/actions/workflows/ci.yml/badge.svg)](https://github.com/lijinbio/visionkb-api/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/lijinbio/visionkb-api?sort=date)](https://github.com/lijinbio/visionkb-api/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-3776AB)](pyproject.toml)

[API reference](docs/api.md) · [OpenAPI](docs/openapi.json) · [Contracts](docs/contracts.md) · [Contribute](CONTRIBUTING.md) · [Security](SECURITY.md)

</div>

---

VisionKB is a Biolink-based knowledge graph for eye research: genes, cell types and states,
anatomy, diseases and phenotypes, with expression evidence summarised from single-cell atlases
and curated gene-disease assertions. This service is the only path to a release for every client
(AiScientist tools, the `visionkb-kg` MCP server, a future web UI). Its functions are restricted
and read-only; the one write, `POST /v1/proposals`, puts an edge into a PostgreSQL curation queue
and never touches the graph.

| Endpoint | Function |
| --- | --- |
| `GET /v1/health`, `GET /v1/release` | component status (no auth); manifest subset |
| `GET /v1/entities/search`, `GET /v1/entities/{curie}` | `search_entities`: exact CURIE, symbol, synonym, then full text |
| `GET /v1/relationships` | `get_relationships`: typed Biolink edges with qualifiers and provenance |
| `GET /v1/evidence/{edge_id}` | `get_supporting_publications`: StudyResults and publications of an edge |
| `GET /v1/gene-sets` | `get_gene_set`: genes expressed in a cell type/state, or associated with a disease/phenotype |
| `GET /v1/atlas/expression` | `query_expression`: gene x group summaries from Parquet (DuckDB) |
| `GET /v1/datasets` | `find_datasets`: dataset registry records with `bind` |
| `POST /v1/proposals`, `GET /v1/proposals` | write-back into the curation queue (separate token scopes) |

Every response is an envelope `{status, kg_release, data, page, warnings, error}`; lists default to
50 rows (max 200) and a response aims at 24,000 characters at most. See [docs/api.md](docs/api.md)
for parameters and real example responses.

## Data it reads

A **release bundle** directory, mounted read-only (`VISIONKB_RELEASE_DIR`), plus Neo4j and
PostgreSQL. Nothing else: the bundle is self-contained and relocatable (relative paths; every file
listed with size and sha256 in `manifest.json`), so the same image runs on an HPC node or a
Kubernetes host with no shared filesystem.

```
<kg_release>/
├── RELEASE, manifest.json
├── neo4j-import/        # nodes_*.csv, rels_*.csv, import.args, post_import.cypher (loaded into Neo4j)
├── summaries/expr_summary.parquet
├── registry/datasets.jsonl
└── kgx/, validation/
```

At startup the API checks that `RELEASE` matches `VISIONKB_KG_RELEASE` (if set) and `manifest.json`,
that every manifest path stays inside the bundle and that sizes match (`VISIONKB_VERIFY_CHECKSUMS=1`
also checks sha256). Dataset paths in the registry (`h5ad_path`, `soma_uri`, `bind.path`) are
returned as metadata and never opened.

## Configuration

| Variable | Meaning |
| --- | --- |
| `VISIONKB_RELEASE_DIR` | release bundle directory (required) |
| `VISIONKB_KG_RELEASE` | expected release id (optional check) |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD_FILE` | Bolt connection; password read from the file |
| `NEO4J_DATABASE` | database name (default `neo4j`) |
| `VISIONKB_PG_DSN_FILE` | PostgreSQL DSN in a file (proposals; schema migrated at startup) |
| `VISIONKB_API_TOKENS_FILE` | bearer tokens, one `token<TAB>scopes` per line; scopes `read`, `proposals:read`, `proposals:write` |
| `VISIONKB_API_HOST`, `VISIONKB_API_PORT` | bind (default `127.0.0.1:8770`; the image uses `0.0.0.0`) |
| `VISIONKB_MAX_ROWS` | row cap, 1..200 (default 200) |
| `VISIONKB_VERIFY_CHECKSUMS` | `1` to verify every bundle file's sha256 at startup |

Secrets are only ever read from files, never from environment values or arguments.

## Install and run

```bash
pip install git+https://github.com/lijinbio/visionkb-api@v0.1.0
export VISIONKB_RELEASE_DIR=/path/to/releases/visionkb-YYYY.MM.N NEO4J_URI=bolt://127.0.0.1:7687 \
       NEO4J_PASSWORD_FILE=/path/to/secrets/neo4j_password VISIONKB_PG_DSN_FILE=/path/to/secrets/pg_dsn \
       VISIONKB_API_TOKENS_FILE=/path/to/secrets/tokens.tsv
visionkb-api check      # load the release, ping Neo4j/PostgreSQL, print /v1/health
visionkb-api serve      # uvicorn on VISIONKB_API_HOST:VISIONKB_API_PORT
visionkb-api openapi --out openapi.json
```

Container image (built and pushed by the release workflow on tags `vX.Y.Z`):

```bash
docker run --rm -p 8770:8770 --read-only --tmpfs /tmp \
  -v /path/to/release:/release:ro -v /path/to/secrets:/run/secrets:ro \
  -e VISIONKB_API_TOKENS_FILE=/run/secrets/tokens.tsv -e NEO4J_URI=bolt://neo4j:7687 \
  -e NEO4J_PASSWORD_FILE=/run/secrets/neo4j_password -e VISIONKB_PG_DSN_FILE=/run/secrets/pg_dsn \
  ghcr.io/lijinbio/visionkb-api:0.1.0
apptainer pull docker://ghcr.io/lijinbio/visionkb-api:0.1.0     # HPC
```

The image runs as uid 10001 with a read-only root filesystem (`/tmp` is the only scratch) and has a
`HEALTHCHECK` on `/v1/health`. Pin it by digest in deployments (see the GitHub release notes).
Deployment (apptainer/Slurm and Kubernetes/docker-compose) lives in `lijinbio/visionkb-deploy`.

## Loading a release into Neo4j

```bash
cd <kg_release>/neo4j-import
neo4j-admin database import full neo4j --overwrite-destination=true @import.args
# start Neo4j, then:
cypher-shell -u neo4j -f post_import.cypher   # constraints, full-text index entity_search, indexes
```

`scripts/local_services.sh` does this with apptainer (no Docker) for the integration tests; CI does
it with `docker run neo4j-admin` against a service container.

## Develop

See [CONTRIBUTING.md](CONTRIBUTING.md): `uv sync && uv run pytest` runs the unit tests on a
generated fixture release; the integration tests need live Neo4j and PostgreSQL.

## License

MIT, see [LICENSE](LICENSE). Neo4j Community Edition, used as the graph store, is GPLv3 and runs as
a separate service.
