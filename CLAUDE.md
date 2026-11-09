# VisionKB API

Read-only FastAPI service over a VisionKB KG release (Neo4j + Parquet summaries + registry) with a
PostgreSQL proposals queue. Package `visionkb_api` under `src/`; contracts in `docs/contracts.md` (§8 API).

- Never invent ids, values or results; errors return an error envelope, never fallback data.
- Cypher: fixed query text, every request value a parameter. Relationship types go in as
  `$any($types)` after checking them against the Biolink predicates present in the release; the only
  spliced identifiers are Biolink predicate names from the vendored list (`graph._rel_type`).
- SQL (DuckDB, PostgreSQL): bound parameters only; sort/level values come from allowlists.
- The release bundle directory is the only file input; never open paths found in the data.
- Secrets only from `*_FILE` paths; never log them.
- Every response: envelope with `kg_release`; default limit 50, max 200, about 24,000 characters (`envelope.fit`).
- After changing an endpoint: `uv run visionkb-api openapi --out docs/openapi.json` and regenerate
  `docs/api.md` with `scripts/make_api_docs.py` (needs the live services).
- Before a commit: `uv run ruff check . && uv run ruff format . && uv run pytest`.
- Hygiene: no personal or cluster paths, user or account names, hostnames, tokens or lab data in any
  committed file; use `/path/to/...`, `<partition>`, `<account>`.
- Version: bump `pyproject.toml` and `src/visionkb_api/__init__.py`, add a CHANGELOG entry, tag `vX.Y.Z`.
