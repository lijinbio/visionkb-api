# Contributing

Participation is covered by the [code of conduct](CODE_OF_CONDUCT.md).

## Quick start

```bash
git clone https://github.com/lijinbio/visionkb-api.git && cd visionkb-api
uv sync && uv run pytest        # or: pip install -e . pytest && pytest
pre-commit install              # optional: run the checks on every commit
```

The unit tests generate the synthetic fixture release (`tests/fixtures/make_fixture.py`) in a
temporary directory and need no database.

## Integration tests (live Neo4j and PostgreSQL)

With apptainer (HPC, no Docker):

```bash
export VISIONKB_WORK_DIR=/path/to/workdir PYTHON=$PWD/.venv/bin/python
scripts/local_services.sh pull       # docker://neo4j:2026.10.1-community, docker://postgres:17.11 -> .sif
scripts/local_services.sh fixture    # fixture release under $VISIONKB_WORK_DIR/visionkb-api/fixtures/
scripts/local_services.sh import     # neo4j-admin database import full (fixture)
scripts/local_services.sh up         # Neo4j + PostgreSQL instances, post_import.cypher
eval "$(scripts/local_services.sh env)" && uv run pytest -rs
scripts/local_services.sh down
```

With Docker, `.github/workflows/ci.yml` (job `integration`) shows the same steps with service containers.

## Conventions

| Topic | Rule |
| --- | --- |
| Contracts | `docs/contracts.md` is a sanitized copy; change the authoritative contract first, then code |
| Queries | parameterized Cypher and SQL only; no request value is ever spliced into a query string |
| Responses | the envelope, `kg_release` on every response, limits and the 24,000-character budget; `/v1/health` always 200, `/v1/ready` 503 unless every component is ok |
| Vocabulary | Biolink 4.4.6, vendored by `scripts/vendor_biolink.py` (records the source sha256) |
| Tests | unit tests on the fixture files; integration tests against live services; no mocks of the databases |
| Docs | `docs/openapi.json` must match the app (checked in CI); regenerate `docs/api.md` when responses change |
| Lint | `ruff check .` and `ruff format --check .` (settings in `pyproject.toml`) |
| Hygiene | no personal paths, emails, real cluster accounts, hostnames, tokens or private data; use `/path/to/...`, `<partition>`, `<account>` |

## Releasing

Bump the version in `pyproject.toml` and `src/visionkb_api/__init__.py`, add a CHANGELOG entry and
push a tag `vX.Y.Z`. The release workflow checks the versions, runs the tests, pushes
`ghcr.io/lijinbio/visionkb-api:X.Y.Z` and publishes a GitHub release with the image digest.
