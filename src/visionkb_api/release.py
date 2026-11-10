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
    "expression_edge_rule",
)
# Older releases (contracts < v0.7) have no expression_edge_rule: they were built with decision D8.
D8_RULE = {"decision": "D8", "min_pct_expressing": 0.10, "min_n_cells": 20}
MAX_LIST_IN_SUBSET = 50
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
    # (dataset_id, group_label) -> [{"id": "KI-1", "program_of": "Rod"}, ...] from manifest known_issues
    known_issue_groups: dict[tuple[str, str], list[dict]] = field(default_factory=dict)

    @property
    def edge_rule(self) -> dict:
        """The expression edge rule of this release (manifest ``expression_edge_rule``), else the D8 rule."""
        rule = self.manifest.get("expression_edge_rule")
        return rule if isinstance(rule, dict) and rule else dict(D8_RULE, fallback=True)

    @property
    def min_pct_for_edges(self) -> float:
        return float(self.edge_rule.get("min_pct_expressing", D8_RULE["min_pct_expressing"]))

    def edge_rule_text(self) -> str:
        """One-line description of when the release has an expressed_in edge, read from the manifest."""
        r = self.edge_rule
        parts = []
        if "min_pct_expressing" in r or "min_n_cells" in r:
            a = []
            if "min_pct_expressing" in r:
                a.append(f"pct_expressing >= {r['min_pct_expressing']}")
            if "min_n_cells" in r:
                a.append(f"n_cells >= {r['min_n_cells']}")
            parts.append(" and ".join(a))
        if "specificity_fraction" in r:
            parts.append(
                f"pseudobulk_cp10k >= {r['specificity_fraction']} x the gene's max in the same dataset and level"
            )
        if "min_n_donors_expressing" in r:
            parts.append(f"n_donors_expressing >= {r['min_n_donors_expressing']} (where donor data exist)")
        text = "; ".join(parts) or "see manifest expression_edge_rule"
        if r.get("levels"):
            text += f"; levels {', '.join(r['levels'])} only"
        source = f"decision {r['decision']}" if r.get("decision") else "manifest expression_edge_rule"
        if r.get("fallback"):
            source = "decision D8; this release's manifest has no expression_edge_rule"
        return f"{text} ({source})"

    def known_issues_for(self, dataset_id: str | None, group_label: str | None, level: str | None = None) -> list[dict]:
        """Known issues flagging this group. Flagged groups are cell-type groups: a ``celltype`` row (or one of
        unknown level) matches by label; a ``majorclass`` row only if the flagged group *is* that major class
        (pair ``group_majorclass`` == label, same cells); ``region_majorclass`` rows never match."""
        tags = self.known_issue_groups.get((dataset_id or "", group_label or ""), [])
        if level == "region_majorclass":
            return []
        if level == "majorclass":
            return [t for t in tags if t.get("group_majorclass") == group_label]
        return tags

    @property
    def summaries_path(self) -> Path:
        return self.path / SUMMARIES

    def manifest_subset(self) -> dict:
        out = {k: self.manifest.get(k) for k in MANIFEST_SUBSET}
        # Source paths are build-host metadata: return provenance fields only.
        out["sources"] = [
            {k: s.get(k) for k in SOURCE_FIELDS if k in s} for s in self.manifest.get("sources", []) or []
        ]
        out["expression_edge_rule_text"] = self.edge_rule_text()
        out["known_issues"] = [
            {k: (v[:MAX_LIST_IN_SUBSET] if isinstance(v, list) else v) for k, v in ki.items()}
            for ki in self.manifest.get("known_issues") or []
            if isinstance(ki, dict)
        ]
        out["n_known_issue_groups"] = len(self.known_issue_groups)
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


def _issue_records(obj) -> list[dict]:
    if isinstance(obj, list):
        return [o for o in obj if isinstance(o, dict)]
    return [obj] if isinstance(obj, dict) else []


def load_known_issue_groups(root: Path, manifest: dict) -> tuple[dict[tuple[str, str], list[dict]], list[str]]:
    """Groups flagged by manifest ``known_issues``: from each entry's ``details`` file (``pairs`` with
    dataset_id/group_label/program_of), else from its ``*_groups`` lists of ``dataset_id|group_label``."""
    groups: dict[tuple[str, str], list[dict]] = {}
    notes: list[str] = []

    def add(ki_id: str, ds: str, label: str, program: str | None, majorclass: str | None = None) -> None:
        tags = groups.setdefault((ds, label), [])
        if not any(t["id"] == ki_id and t.get("program_of") == program for t in tags):
            tag = {"id": ki_id}
            if program:
                tag["program_of"] = program
            if majorclass:
                tag["group_majorclass"] = majorclass
            tags.append(tag)

    for entry in manifest.get("known_issues") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        ki_id, found = str(entry["id"]), False
        details = entry.get("details")
        if isinstance(details, str) and details.endswith(".json"):
            try:
                for rec in _issue_records(json.loads(_inside(root, details).read_text(encoding="utf-8"))):
                    if rec.get("id", ki_id) != ki_id:
                        continue
                    for p in rec.get("pairs") or []:
                        if p.get("dataset_id") and p.get("group_label"):
                            add(
                                ki_id, p["dataset_id"], p["group_label"], p.get("program_of"), p.get("group_majorclass")
                            )
                            found = True
            except (OSError, ValueError, ConfigError) as exc:
                notes.append(f"{ki_id}: details {details} not read ({exc})")
        if not found:
            for key, val in entry.items():
                if key.endswith("_groups") and isinstance(val, list):
                    program = key.removesuffix("_groups").removesuffix("_program").capitalize() or None
                    for item in val:
                        if isinstance(item, str) and "|" in item:
                            ds, label = item.split("|", 1)
                            add(ki_id, ds, label, program)
    return groups, notes


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
    ki_groups, ki_notes = load_known_issue_groups(path, manifest)
    if ki_notes:
        checks["known_issues"] = ki_notes
    return Release(
        path=path, kg_release=rid, manifest=manifest, datasets=datasets, checks=checks, known_issue_groups=ki_groups
    )
