# Security policy

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/lijinbio/visionkb-api/security/advisories/new)
instead. We aim to respond within five working days.

Report, for example:

- a request that changes the graph, or reaches Cypher or SQL other than as a parameter;
- a response that reveals data outside the release, a secret, or a path the service should not open;
- an endpoint that answers without the token scope it documents;
- a leaked credential, personal path, cluster account or private dataset reference in the repository;
- a container image that doesn't match the code at its tag.

## What the service guarantees

- Neo4j is only read; the only write is the proposals queue in PostgreSQL.
- Request values reach Cypher and SQL only as parameters; CURIEs, predicates, categories and enums
  are validated first.
- Secrets are read from files, never from environment values or arguments, and never logged; tokens
  are kept in memory as sha256 digests.
- The image runs as a non-root user and works with a read-only root filesystem.

Run it with the least access it needs: mount the release read-only, give the Neo4j user no write
role where the edition allows it, and pin the image by digest.
