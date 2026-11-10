# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) and are released with the tag `vX.Y.Z`.

## [Unreleased]

## [0.1.4] - 2026-11-09

### Changed

- Test fixture manifest follows contracts v0.7 §6 counts: `nodes_by_category` counts each node once under
  its most specific category (CellState nodes no longer also under `biolink:Cell`) and sums to the new
  `nodes_total`; `edges_by_predicate` sums to the new `edges_total`; `counts.neo4j` gives nodes per label
  file and relationships per type, as in real releases. visionkb-deploy compares these totals with
  neo4j-admin's import. The fixture id stays `visionkb-0000.01.0` (`visionkb-0000.01.1`/`.2` are
  visionkb-deploy's own fixtures; nothing pins the old checksum). No service code change.

## [0.1.3] - 2026-11-09

### Changed

- `/v1/gene-sets` `kind=expressed` ranks by specificity by default: score = (pseudobulk_cp10k of the
  supporting group + 1) / (max pseudobulk_cp10k of the gene over the other groups of the same dataset
  and level + 1), best StudyResult per gene, ties by pct_expressing. The pseudocount of 1 CP10k is the
  offset of the summaries' log1p(CP10k) scale. Check on `visionkb-2026.11.0` (HRCA snRNA, Rod majorclass,
  5477 genes meeting D18; summaries only, not the graph): by pct MALAT1 is 1st; by specificity MALAT1 is
  5170th and PDE6A/SAG/NR2E3/RHO are 1/2/4/10. With pseudocount 0.1: MALAT1 4665th, PDE6A/SAG/NR2E3/RHO
  1/4/5/25 (lncRNA/clone genes AC130415.1 and AC079362.1 at 2nd-3rd). With 0.01: MALAT1 4533th,
  PDE6A/SAG/NR2E3/RHO 6/10/7/48, and low-abundance lncRNA/clone genes lead (top five: AC130415.1, AC104938.1,
  GUCY2F, AC079362.1, LINC02733). In short: MALAT1 is demoted at all three pseudocounts; rod markers lead at
  1 and 0.1; at 0.01 low-abundance lncRNAs lead, which is why 1 CP10k is used. Items carry `score_kind`, `pct_expressing`, `best_study_result` (group, pseudobulk,
  other-group max and group) and per-edge `specificity`; the definition is in `warnings`. `sort=pct` keeps
  the previous ranking. Other-group maxima come from a top-2 table per (dataset, level, gene) built from
  the summaries at startup (0.5 s on the 26.8M-row `visionkb-2026.11.0` parquet).
- `/v1/datasets` `tissue` also matches UBERON terms that are `part_of` / `subclass_of` it in the release
  graph (up to 6 hops; `expand=false` for exact matching); the expanded term list is in `warnings` and
  each record has `matched_tissue`.
- Fixture: `UBERON:0001781` (layer of retina) with `UBERON:0001782 subclass_of UBERON:0001781` and
  `UBERON:0001781 part_of UBERON:0000966` (OLS4).

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
