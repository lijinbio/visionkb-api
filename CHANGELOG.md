# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) and are released with the tag `vX.Y.Z`.

## [Unreleased]

## [0.1.0] - 2026-11-09

### Added

- Read-only API per contracts v0.3 §8: health, release, entity search (exact CURIE, symbol,
  synonym, full text) and detail, relationships, evidence, gene sets, atlas expression (DuckDB over
  Parquet), dataset search with bind records; proposals queue in PostgreSQL (schema migrated at
  startup) with separate `proposals:read` / `proposals:write` scopes.
- Response envelope with `kg_release`, limits (50 default, 200 max) and a 24,000-character budget.
- Release bundle checks: relative manifest paths inside the bundle, sizes, optional sha256.
- Biolink 4.4.6 vocabulary vendored with its source checksum (`scripts/vendor_biolink.py`).
- Synthetic fixture release generator (real, verified identifiers; synthetic values) that writes
  the contract files, `neo4j-import/` and a relocatable bundle tar with sha256.
- Unit and integration tests (live Neo4j 2026.10.1 Community and PostgreSQL 17.11); apptainer helper
  `scripts/local_services.sh`; CI (lint, tests on Python 3.12/3.13, integration with service
  containers, pip install, container build and smoke test); release workflow pushing
  `ghcr.io/lijinbio/visionkb-api:<version>`.
- Docs: `docs/api.md` (generated from real fixture responses), `docs/openapi.json`, `docs/contracts.md`.
