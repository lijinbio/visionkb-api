"""A KG release bundle directory (contracts v0.3 §1, §4, §6).

The bundle is self-contained and relocatable: ``RELEASE``, ``manifest.json`` (every file with
its relative path, size and sha256), ``summaries/``, ``registry/`` and ``neo4j-import/``. The API
reads nothing outside it. Registry paths (``h5ad_path``, ``soma_uri``, ``bind.path``) are
returned as data and never opened.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import ConfigError

# Contract §1: kg_release = visionkb-YYYY.MM.N (the test fixture uses visionkb-0000.01.0).
RELEASE_ID_RE = re.compile(r"^visionkb-\d{4}\.\d{2}\.\d+$")
MANIFEST_SUBSET = (
    "kg_release",
    "created_utc",
    "biolink_version",
    "schema_version",
    "code",
    "atlases",
    "counts",
    "validation",
)
SOURCE_FIELDS = ("name", "version", "date", "license", "infores", "sha256")
SUMMARIES = "summaries/expr_summary.parquet"
REGISTRY = "registry/datasets.jsonl"


@dataclass
class Release:
    path: Path
    kg_release: str
    manifest: dict
    datasets: list[dict] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    @property
    def summaries_path(self) -> Path:
        return self.path / SUMMARIES

    def manifest_subset(self) -> dict:
        out = {k: self.manifest.get(k) for k in MANIFEST_SUBSET}
        # Source paths are build-host metadata: return provenance fields only.
        out["sources"] = [
            {k: s.get(k) for k in SOURCE_FIELDS if k in s} for s in self.manifest.get("sources", []) or []
        ]
        out["n_files"] = len(self.manifest.get("files") or [])
        out["n_datasets"] = len(self.datasets)
        out["bundle_checks"] = self.checks
        return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _inside(root: Path, rel: str) -> Path:
    """Resolve a manifest path; it must be relative and stay inside the bundle (no ``..``, no symlink out)."""
    pp = PurePosixPath(rel)
    if pp.is_absolute() or ".." in pp.parts or not rel:
        raise ConfigError(f"manifest file path {rel!r} is not a relative path inside the bundle")
    full = (root / pp).resolve()
    if not full.is_relative_to(root.resolve()):
        raise ConfigError(f"manifest file {rel!r} resolves outside the bundle")
    return full


def check_files(root: Path, manifest: dict, *, verify_checksums: bool) -> dict:
    """Check every manifest file: relative path, present, size; sha256 too if requested."""
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ConfigError("manifest.json lists no files")
    listed = set()
    for f in files:
        rel = f.get("path", "")
        full = _inside(root, rel)
        listed.add(rel)
        if not full.is_file():
            raise ConfigError(f"bundle file missing: {rel}")
        if f.get("bytes") is not None and full.stat().st_size != int(f["bytes"]):
            raise ConfigError(f"bundle file size differs from manifest: {rel}")
        if verify_checksums and f.get("sha256") and _sha256(full) != f["sha256"]:
            raise ConfigError(f"bundle file checksum differs from manifest: {rel}")
    for needed in (SUMMARIES, REGISTRY):
        if needed not in listed:
            raise ConfigError(f"manifest.json does not list {needed}")
    return {"n_files": len(files), "sizes": "ok", "sha256": "ok" if verify_checksums else "not_checked"}


def load_release(path: Path | None, expected: str | None = None, *, verify_checksums: bool = False) -> Release:
    """Load and check a release bundle directory. Raises ConfigError if it is missing or inconsistent."""
    if path is None:
        raise ConfigError("VISIONKB_RELEASE_DIR is not set")
    if not path.is_dir():
        raise ConfigError(f"release directory not found: {path}")
    try:
        rid = (path / "RELEASE").read_text(encoding="utf-8").strip()
    except OSError:
        raise ConfigError("RELEASE file is missing from the release directory") from None
    if not RELEASE_ID_RE.match(rid):
        raise ConfigError(f"invalid release id in RELEASE: {rid!r} (expected visionkb-YYYY.MM.N)")
    if expected and rid != expected:
        raise ConfigError(f"RELEASE is {rid!r} but VISIONKB_KG_RELEASE is {expected!r}")
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read manifest.json: {exc}") from None
    if manifest.get("kg_release") != rid:
        raise ConfigError(f"manifest.json kg_release {manifest.get('kg_release')!r} != RELEASE {rid!r}")
    checks = check_files(path, manifest, verify_checksums=verify_checksums)
    datasets: list[dict] = []
    for n, line in enumerate(_inside(path, REGISTRY).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                datasets.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{REGISTRY} line {n}: {exc}") from None
    return Release(path=path, kg_release=rid, manifest=manifest, datasets=datasets, checks=checks)
