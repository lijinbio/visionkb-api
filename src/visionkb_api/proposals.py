"""Curation queue in PostgreSQL. Proposals are stored, never written to Neo4j."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from typing import Any

from .envelope import ApiError

# Ordered migrations; each runs once, recorded in visionkb_schema_migrations.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS proposals (
            proposal_id   text PRIMARY KEY,
            created_utc   timestamptz NOT NULL DEFAULT now(),
            status        text NOT NULL DEFAULT 'proposed'
                          CHECK (status IN ('proposed', 'accepted', 'rejected', 'withdrawn')),
            kg_release    text NOT NULL,
            run_id        text NOT NULL,
            visibility    text NOT NULL CHECK (visibility IN ('owner', 'lab', 'public')),
            submitted_by  text NOT NULL,
            subject       text NOT NULL,
            predicate     text NOT NULL,
            object        text NOT NULL,
            edge_sha256   text NOT NULL,
            edge          jsonb NOT NULL,
            evidence      jsonb NOT NULL DEFAULT '[]'::jsonb,
            review_note   text,
            reviewed_utc  timestamptz,
            UNIQUE (run_id, edge_sha256)
        );
        CREATE INDEX IF NOT EXISTS proposals_status_idx ON proposals (status, created_utc DESC);
        CREATE INDEX IF NOT EXISTS proposals_subject_idx ON proposals (subject);
        CREATE INDEX IF NOT EXISTS proposals_object_idx ON proposals (object);
        """,
    ),
]
STATUSES = ("proposed", "accepted", "rejected", "withdrawn")
_LOCK_KEY = 7_406_515_001  # advisory lock id for migrations


def edge_sha256(edge: dict) -> str:
    return hashlib.sha256(json.dumps(edge, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProposalStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._migrated = False
        self._lock = threading.Lock()

    def _connect(self):
        import psycopg

        try:
            return psycopg.connect(self._dsn, connect_timeout=5, application_name="visionkb-api")
        except psycopg.OperationalError:
            raise ApiError(503, "postgres_unavailable", "the proposals database is not reachable") from None

    def migrate(self) -> int:
        """Apply pending migrations under an advisory lock. Returns the schema version."""
        with self._lock, self._connect() as con, con.transaction():
            con.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            con.execute(
                "CREATE TABLE IF NOT EXISTS visionkb_schema_migrations ("
                "version integer PRIMARY KEY, applied_utc timestamptz NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in con.execute("SELECT version FROM visionkb_schema_migrations")}
            for version, sql in MIGRATIONS:
                if version not in done:
                    con.execute(sql)
                    con.execute("INSERT INTO visionkb_schema_migrations (version) VALUES (%s)", (version,))
            self._migrated = True
            return max(v for v, _ in MIGRATIONS)

    def _ensure(self) -> None:
        if not self._migrated:
            self.migrate()

    def ping(self) -> dict:
        with self._connect() as con:
            version = con.execute("SHOW server_version").fetchone()[0]
            schema = None
            if self._migrated:
                schema = con.execute("SELECT max(version) FROM visionkb_schema_migrations").fetchone()[0]
        return {"server_version": version, "schema_version": schema}

    def submit(
        self, *, edge: dict, evidence: list[dict], run_id: str, visibility: str, submitted_by: str, kg_release: str
    ) -> tuple[str, bool]:
        """Insert a proposal. Returns (proposal_id, created); an identical (run_id, edge) returns the first id."""
        from psycopg.types.json import Jsonb

        self._ensure()
        digest = edge_sha256(edge)
        pid = "visionkb:p-" + uuid.uuid4().hex
        with self._connect() as con, con.transaction():
            row = con.execute(
                "INSERT INTO proposals (proposal_id, kg_release, run_id, visibility, submitted_by, subject, predicate,"
                " object, edge_sha256, edge, evidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (run_id, edge_sha256) DO NOTHING RETURNING proposal_id",
                (
                    pid,
                    kg_release,
                    run_id,
                    visibility,
                    submitted_by,
                    edge["subject"],
                    edge["predicate"],
                    edge["object"],
                    digest,
                    Jsonb(edge),
                    Jsonb(evidence),
                ),
            ).fetchone()
            if row:
                return row[0], True
            existing = con.execute(
                "SELECT proposal_id FROM proposals WHERE run_id = %s AND edge_sha256 = %s", (run_id, digest)
            ).fetchone()
            return existing[0], False

    def list(self, *, status: str | None, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        self._ensure()
        where, params = ("WHERE status = %s", [status]) if status else ("", [])
        with self._connect() as con:
            total = con.execute(f"SELECT count(*) FROM proposals {where}", params).fetchone()[0]  # noqa: S608
            cur = con.execute(
                "SELECT proposal_id, created_utc, status, kg_release, run_id, visibility, submitted_by,"  # noqa: S608
                f" subject, predicate, object, edge, evidence, review_note FROM proposals {where}"
                " ORDER BY created_utc DESC, proposal_id LIMIT %s OFFSET %s",
                [*params, limit, offset],
            )
            names = [d.name for d in cur.description]
            rows = []
            for r in cur.fetchall():
                row = dict(zip(names, r, strict=True))
                row["created_utc"] = row["created_utc"].isoformat()
                rows.append(row)
        return rows, int(total)
