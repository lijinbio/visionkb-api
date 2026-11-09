"""Response envelope (contracts §8) and the per-response character budget."""

from __future__ import annotations

import json
from typing import Any


class ApiError(Exception):
    """An error returned to the client as an ``{"status": "error"}`` envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def dumps(obj: Any) -> str:
    # Same separators as Starlette's JSONResponse, so the measured size is the sent size.
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str)


def envelope(
    kg_release: str,
    data: Any = None,
    *,
    page: dict | None = None,
    warnings: list[str] | None = None,
    error: dict | None = None,
) -> dict:
    return {
        "status": "error" if error else "ok",
        "kg_release": kg_release,
        "data": data,
        "page": page,
        "warnings": list(warnings or []),
        "error": error,
    }


def _trim_list(env: dict, get, put, label: str, max_chars: int) -> None:
    items = get()
    full = len(items)
    note = f"{label}: returned {{n}} of {full} items to stay within {max_chars} characters; page with limit/offset"
    env["warnings"].append(note.format(n=full))
    lo, hi = 0, full
    while lo < hi:  # largest n with size(n) <= max_chars
        mid = (lo + hi + 1) // 2
        put(items[:mid])
        env["warnings"][-1] = note.format(n=mid)
        if len(dumps(env)) <= max_chars:
            lo = mid
        else:
            hi = mid - 1
    put(items[:lo])
    env["warnings"][-1] = note.format(n=lo)


def fit(env: dict, max_chars: int) -> dict:
    """Trim the largest list in ``data`` until the serialized envelope fits ``max_chars``.

    Lists are cut from the end (results are already ranked) and a warning says how many items
    were returned, so the client can page on. Scalars are never altered.
    """
    if len(dumps(env)) <= max_chars:
        return env
    data = env.get("data")
    if isinstance(data, list):

        def put(v):
            env["data"] = v

        _trim_list(env, lambda: data, put, "data", max_chars)
    elif isinstance(data, dict):
        lists = sorted((k for k, v in data.items() if isinstance(v, list) and v), key=lambda k: -len(dumps(data[k])))
        for key in lists:
            if len(dumps(env)) <= max_chars:
                break
            original = data[key]

            def put(v, key=key):
                data[key] = v

            _trim_list(env, lambda original=original: original, put, f"data.{key}", max_chars)
    if len(dumps(env)) > max_chars:
        env["warnings"].append(f"response is larger than {max_chars} characters after trimming lists")
    return env
