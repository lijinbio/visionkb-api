"""Settings from environment variables (contracts §9). Secrets are read from ``*_FILE`` paths only."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIMIT = 50
HARD_MAX_ROWS = 200
MAX_CHARS = 24_000
MAX_OFFSET = 10_000


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def read_secret(path: Path | str) -> str:
    """Return the first line of a secret file, without the newline. Never log the value."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read secret file {p.name!r}: {exc.strerror}") from None
    value = text.splitlines()[0].strip() if text.strip() else ""
    if not value:
        raise ConfigError(f"secret file {p.name!r} is empty")
    return value


def _path(env: Mapping[str, str], name: str) -> Path | None:
    v = env.get(name, "").strip()
    return Path(v) if v else None


@dataclass(frozen=True)
class Settings:
    release_dir: Path | None = None
    kg_release: str | None = None
    neo4j_uri: str | None = None
    neo4j_user: str = "neo4j"
    neo4j_password_file: Path | None = None
    neo4j_database: str = "neo4j"
    pg_dsn_file: Path | None = None
    tokens_file: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8770
    max_rows: int = HARD_MAX_ROWS
    max_chars: int = MAX_CHARS
    neo4j_timeout_s: float = 15.0
    verify_checksums: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        # The release bundle directory is the only data input (contracts v0.3 §1, D16): no
        # fallback to VISIONKB_DATA_ROOT, which does not exist on the production host.
        release = env.get("VISIONKB_KG_RELEASE", "").strip() or None
        release_dir = _path(env, "VISIONKB_RELEASE_DIR")
        try:
            port = int(env.get("VISIONKB_API_PORT", "8770"))
            max_rows = int(env.get("VISIONKB_MAX_ROWS", str(HARD_MAX_ROWS)))
        except ValueError as exc:
            raise ConfigError(f"invalid integer setting: {exc}") from None
        if not 1 <= max_rows <= HARD_MAX_ROWS:
            raise ConfigError(f"VISIONKB_MAX_ROWS must be 1..{HARD_MAX_ROWS}")
        return cls(
            release_dir=release_dir,
            kg_release=release,
            neo4j_uri=env.get("NEO4J_URI", "").strip() or None,
            neo4j_user=env.get("NEO4J_USER", "neo4j").strip() or "neo4j",
            neo4j_password_file=_path(env, "NEO4J_PASSWORD_FILE"),
            neo4j_database=env.get("NEO4J_DATABASE", "neo4j").strip() or "neo4j",
            pg_dsn_file=_path(env, "VISIONKB_PG_DSN_FILE"),
            tokens_file=_path(env, "VISIONKB_API_TOKENS_FILE"),
            host=env.get("VISIONKB_API_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port,
            max_rows=max_rows,
            verify_checksums=env.get("VISIONKB_VERIFY_CHECKSUMS", "0").strip().lower() in {"1", "true", "yes"},
        )
