"""Expression summaries (contracts §3) queried with DuckDB straight from Parquet."""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

COLUMNS = (
    "dataset_id",
    "atlas_version",
    "level",
    "group_label",
    "cell_type_id",
    "region",
    "gene_id",
    "gene_symbol",
    "n_cells",
    "n_donors",
    "n_expressing",
    "pct_expressing",
    "mean_log1p_cp10k",
    "pseudobulk_cp10k",
    "n_donors_expressing",
)
LEVELS = ("majorclass", "celltype", "region_majorclass")
SORTS = ("pseudobulk_cp10k", "mean_log1p_cp10k", "pct_expressing", "n_cells")
MAX_GENES = 20


# SQL text below is built only from fixed fragments, column names in COLUMNS and the allowlisted
# SORTS; every request value is a bound parameter (S608 is suppressed for that reason).


class Summaries:
    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"summaries file not found: {path.name}")
        self.path = path
        # No extension downloads at run time (offline hosts, read-only root filesystem); the
        # Parquet reader is built into the duckdb wheel.
        self._con = duckdb.connect(":memory:", config={"autoinstall_known_extensions": False})
        self._lock = threading.Lock()
        literal = str(path).replace("'", "''")  # a configured path, not user input
        self._con.execute(f"CREATE VIEW expr AS SELECT * FROM read_parquet('{literal}')")  # noqa: S608
        cols = {r[0] for r in self._con.execute("DESCRIBE expr").fetchall()}
        missing = [c for c in COLUMNS if c not in cols]
        if missing:
            raise ValueError(f"expr_summary.parquet lacks columns {missing}")
        self.has_var_symbol = "var_symbol" in cols
        self.n_rows = self._con.execute("SELECT count(*) FROM expr").fetchone()[0]
        # Per (dataset, level, gene): the two highest group pseudobulk values and their groups, so the
        # max over the *other* groups of any group is a lookup (gene-set specificity). Graph levels only.
        self._con.execute(
            "CREATE TABLE top2 AS SELECT dataset_id, level, gene_id, max(pseudobulk_cp10k, 2) AS pb, "
            "arg_max(group_label, pseudobulk_cp10k, 2) AS grp FROM expr "
            "WHERE level IN ('majorclass', 'celltype') GROUP BY ALL"
        )

    def _cur(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            return self._con.cursor()

    def resolve_genes(self, genes: list[str]) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
        """Map each query (gene id or symbol, case-insensitive) to the (gene_id, symbol) pairs present."""
        cur = self._cur()
        found: dict[str, list[tuple[str, str]]] = {}
        missing: list[str] = []
        sym_col = "var_symbol" if self.has_var_symbol else "gene_symbol"
        for g in genes:
            rows = cur.execute(
                f"SELECT DISTINCT gene_id, gene_symbol FROM expr WHERE gene_id = ? OR lower(gene_symbol) = lower(?) "  # noqa: S608
                f"OR lower({sym_col}) = lower(?) ORDER BY gene_id LIMIT 10",
                [g, g, g],
            ).fetchall()
            if rows:
                found[g] = [(r[0], r[1]) for r in rows]
            else:
                missing.append(g)
        return found, missing

    def expression(
        self,
        gene_ids: list[str],
        *,
        dataset: str | None,
        level: str,
        cell_type: str | None,
        sort: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        if sort not in SORTS or level not in LEVELS:
            raise ValueError("invalid sort or level")
        where = ["gene_id IN (SELECT unnest(?))", "level = ?"]
        params: list = [gene_ids, level]
        if dataset:
            where.append("dataset_id = ?")
            params.append(dataset)
        if cell_type:
            where.append("(cell_type_id = ? OR lower(group_label) = lower(?))")
            params += [cell_type, cell_type]
        clause = " AND ".join(where)
        cur = self._cur()
        total = cur.execute(f"SELECT count(*) FROM expr WHERE {clause}", params).fetchone()[0]  # noqa: S608
        extra = ", var_symbol" if self.has_var_symbol else ""
        rel = cur.execute(
            f"SELECT {', '.join(COLUMNS)}{extra} FROM expr WHERE {clause} "  # noqa: S608
            f"ORDER BY {sort} DESC NULLS LAST, gene_symbol, dataset_id, group_label LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        names = [d[0] for d in rel.description]
        rows = []
        for r in rel.fetchall():
            row = dict(zip(names, r, strict=True))
            row["dataset_version"] = row["atlas_version"]
            for k in ("pct_expressing", "mean_log1p_cp10k", "pseudobulk_cp10k"):
                if row[k] is not None:
                    row[k] = round(float(row[k]), 6)
            rows.append(row)
        return rows, int(total)

    def other_group_max(self, gene_ids: list[str]) -> dict[tuple[str, str, str], tuple[list[float], list[str]]]:
        """(dataset_id, level, gene_id) -> ([top pseudobulk, second], [their group labels]) for these genes."""
        rows = (
            self._cur()
            .execute(
                "SELECT dataset_id, level, gene_id, pb, grp FROM top2 WHERE gene_id IN (SELECT unnest(?))", [gene_ids]
            )
            .fetchall()
        )
        return {(d, lv, g): (list(pb), list(grp)) for d, lv, g, pb, grp in rows}

    def datasets_expressing(self, gene_id: str, min_pct: float = 0.1) -> dict[str, float]:
        """dataset_id -> max pct_expressing of ``gene_id`` over groups with pct >= ``min_pct``."""
        rows = (
            self._cur()
            .execute(
                "SELECT dataset_id, max(pct_expressing) FROM expr WHERE gene_id = ? AND pct_expressing >= ? "
                "GROUP BY dataset_id",
                [gene_id, min_pct],
            )
            .fetchall()
        )
        return {r[0]: float(r[1]) for r in rows}
