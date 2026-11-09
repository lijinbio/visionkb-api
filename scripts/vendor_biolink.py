"""Vendor the Biolink Model vocabulary the API validates against.

Writes ``src/visionkb_api/data/biolink-<version>.json`` with the predicates (descendants of
``related_to``), categories (descendants of ``named_thing``), qualifier slots and the
``KnowledgeLevelEnum`` / ``AgentTypeEnum`` values of one Biolink release, plus the source URL
and sha256 of the YAML it was read from. Needs PyYAML (``pip install pyyaml``)::

    python scripts/vendor_biolink.py --version 4.4.6
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/biolink/biolink-model/v{version}/biolink-model.yaml"


def _ancestors(name: str, table: dict) -> list[str]:
    out, cur = [], name
    while cur and cur in table and cur not in out:
        out.append(cur)
        cur = (table[cur] or {}).get("is_a")
    return out


def _snake(name: str) -> str:
    return name.replace(" ", "_").replace(",", "")


def _pascal(name: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in name.replace(",", "").split(" "))


def build(raw: bytes, url: str) -> dict:
    import yaml

    model = yaml.safe_load(raw)
    slots, classes, enums = model["slots"], model["classes"], model["enums"]
    preds = sorted(s for s in slots if "related to" in _ancestors(s, slots))
    cats = sorted(c for c in classes if "named thing" in _ancestors(c, classes))
    # Qualifier slots: descendants of `qualifier`, plus `* qualifier` association slots such as
    # `quantifier qualifier`, which Biolink 4.4.6 places directly under `association slot`.
    quals = sorted(s for s in slots if "qualifier" in _ancestors(s, slots) or s.endswith(" qualifier"))
    return {
        "biolink_version": str(model["version"]),
        "source_url": url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "predicates": {
            f"biolink:{_snake(p)}": {
                "deprecated": bool(slots[p].get("deprecated")),
                "mixin": bool(slots[p].get("mixin")),
                "abstract": bool(slots[p].get("abstract")),
                "inverse": f"biolink:{_snake(slots[p]['inverse'])}" if slots[p].get("inverse") else None,
            }
            for p in preds
        },
        "categories": {
            f"biolink:{_pascal(c)}": {
                "mixin": bool(classes[c].get("mixin")),
                "abstract": bool(classes[c].get("abstract")),
                "ancestors": [f"biolink:{_pascal(a)}" for a in _ancestors(c, classes)],
            }
            for c in cats
        },
        "qualifiers": [_snake(q) for q in quals],
        "knowledge_level": sorted(enums["KnowledgeLevelEnum"]["permissible_values"]),
        "agent_type": sorted(enums["AgentTypeEnum"]["permissible_values"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", default="4.4.6")
    ap.add_argument("--yaml", type=Path, help="read a local biolink-model.yaml instead of downloading")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "src/visionkb_api/data")
    a = ap.parse_args()
    url = URL.format(version=a.version)
    if a.yaml:
        raw = a.yaml.read_bytes()
    else:
        with urllib.request.urlopen(url, timeout=60) as r:
            raw = r.read()
    data = build(raw, url)
    if data["biolink_version"] != a.version:
        raise SystemExit(f"YAML is Biolink {data['biolink_version']}, expected {a.version}")
    out = a.out / f"biolink-{a.version}.json"
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"{out.name}: {len(data['predicates'])} predicates, {len(data['categories'])} categories")


if __name__ == "__main__":
    main()
