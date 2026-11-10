# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) and are released with the tag `vX.Y.Z`.

## [Unreleased]

## [0.1.2] - 2026-11-09

### Changed

- The expression edge rule is read from the release manifest (`expression_edge_rule`, decision D18:
  pct_expressing >= 0.10 and n_cells >= 20; pseudobulk_cp10k >= 0.10 x the gene's max in the same dataset
  and level; n_donors_expressing >= 2; majorclass/celltype only). `/v1/gene-sets` quotes it in a warning on
  every `kind=expressed` answer and compares `min_pct` with the manifest value; releases without the field
  fall back to the D8 text, labelled as such. No hard-coded D8 text remains.
- The test fixture builds its edges with D18 (15 `expressed_in` edges; RHO->Cone fails rule b, ABCA4->S_Cone
  rule c), records `expression_edge_rule`, a synthetic KI-1 and per-rule exclusion counts
  (`validation/report.json` `expression_edge_rule_exclusions`).

### Added

- `/v1/release` returns `expression_edge_rule`, `expression_edge_rule_text`, `known_issues` and
  `n_known_issue_groups`.
- Known-issue groups (from each manifest `known_issues` entry's `details` file `pairs`, else its
  `*_groups` list): `/v1/atlas/expression` rows and `/v1/evidence` StudyResults in a flagged
  (dataset_id, group_label) carry `known_issues: [ids]`, with one warning per issue. Flagged groups are
  cell-type groups: `celltype` rows match by label, `majorclass` rows only when the flagged group is that
  whole major class (pair `group_majorclass` == label; 11 of the 47 KI-1 groups in `visionkb-2026.11.0`),
  `region_majorclass` rows never.

## [0.1.1] - 2026-11-09

### Added

- `GET /v1/ready` (no auth): HTTP 200 only when the release is loaded and Neo4j, summaries and PostgreSQL
  are all ok; otherwise HTTP 503, `error.code` `not_ready`, failing components in `error.message` and
  `warnings`. `/v1/health` is unchanged (always 200, degraded components in `warnings`).
- CI runs the built image against the live Neo4j/PostgreSQL service containers and requires `/v1/ready`
  200 and a healthy HEALTHCHECK; the no-database smoke test requires 503 and an unhealthy HEALTHCHECK.

### Changed

- Dockerfile `HEALTHCHECK` probes `/v1/ready` instead of `/v1/health`; `visionkb-api check` exits 0 only
  when `/v1/ready` is 200.
- `RELEASE` must match the contract pattern `visionkb-YYYY.MM.N`. The test fixture release is now
  `visionkb-0000.01.0` (was `visionkb-2026.11.0-fixture`), bundle `visionkb-0000.01.0.tar` + `.sha256`.

### Documentation

- Tokens file format: a TAB between token and scopes; scopes separated by commas and/or whitespace
  (`token<TAB>read`, `token<TAB>read,proposals:read`), `#` comments, tokens of 16+ characters.

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
