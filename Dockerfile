# ghcr.io/lijinbio/visionkb-api:<version>
# Build from the repository root:  docker build -t visionkb-api:dev .
# Run (release bundle mounted read-only; secrets as files):
#   docker run --rm -p 8770:8770 --read-only --tmpfs /tmp \
#     -v /path/to/release:/release:ro -v /path/to/secrets:/run/secrets:ro \
#     -e VISIONKB_RELEASE_DIR=/release -e VISIONKB_API_TOKENS_FILE=/run/secrets/tokens.tsv \
#     -e NEO4J_URI=bolt://neo4j:7687 -e NEO4J_PASSWORD_FILE=/run/secrets/neo4j_password \
#     -e VISIONKB_PG_DSN_FILE=/run/secrets/pg_dsn visionkb-api:dev
FROM python:3.15.0-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install uv==0.13.7
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# Dependencies pinned (with hashes) from uv.lock, then the package itself without dependencies.
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && pip install --prefix=/install --require-hashes -r /tmp/requirements.txt \
    && pip install --prefix=/install --no-deps .

FROM python:3.15.0-slim-trixie
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/lijinbio/visionkb-api" \
      org.opencontainers.image.description="VisionKB read-only API over a knowledge-graph release" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=build /install /usr/local
RUN groupadd --system --gid 10001 visionkb \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin visionkb \
    && python -c "import visionkb_api, duckdb, neo4j, psycopg; print('visionkb-api', visionkb_api.__version__)"
# Runs with a read-only root filesystem: no bytecode writes; /tmp is the only scratch (tmpfs/emptyDir).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    VISIONKB_API_HOST=0.0.0.0 \
    VISIONKB_API_PORT=8770 \
    VISIONKB_RELEASE_DIR=/release
USER 10001:10001
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('VISIONKB_API_PORT', '8770') + '/v1/health', timeout=4)"]
CMD ["visionkb-api", "serve"]
